import os

import pandas as pd
import rpy2.robjects as ro
from rpy2.robjects import pandas2ri
from rpy2.robjects.packages import importr

pandas2ri.activate()

ro.r('library(BiocManager)')
# Load required R packages
deseq2 = importr('DESeq2')
edgeR = importr('edgeR')


# TODO make sure every pkg is installed


def __subset_matrix(parameters, treatment_group, control_group):
    samples_in_treatment = parameters.sample_file[parameters.sample_file["groupID"] == treatment_group][
        "sampleID"].values
    samples_in_control = parameters.sample_file[parameters.sample_file["groupID"] == control_group]["sampleID"].values
    relevant_count_matrix_subset = parameters.count_matrix[[*samples_in_treatment, *samples_in_control]]
    relevant_count_matrix_subset.index = parameters.count_matrix['gene_ID']
    return relevant_count_matrix_subset, samples_in_treatment, samples_in_control


def __filter_matrix_on_cpm_threshold(rnaseq_matrix, cpm_genecount_min_threshold=2):
    r_rnaseqMatrix = pandas2ri.py2rpy(rnaseq_matrix)
    normed_r_rnaseqMatrix = edgeR.cpm(r_rnaseqMatrix)
    normed_r_rnaseqMatrix = pd.DataFrame(normed_r_rnaseqMatrix,
                                         columns=rnaseq_matrix.columns,
                                         index=rnaseq_matrix.index
                                         )

    # filter the ORIGINAL matrix -- select only gene with sufficient normed read count
    normed_readcount_for_genes = (normed_r_rnaseqMatrix.values > 1).sum(axis=1)
    filtered_r_rnaseqMatrix = rnaseq_matrix.loc[normed_readcount_for_genes >= cpm_genecount_min_threshold]

    return filtered_r_rnaseqMatrix


def perform_dge(parameters):
    to_compare = parameters.contrasts
    full_results = []  # treatment, control, deseq, edger
    for i, row in to_compare.iterrows():
        treatment_group = row["treatment"]
        control_group = row['control']
        print(f"Running analysis with treatment group {treatment_group} and control group {control_group}")
        matrix_subset, samples_in_treatment, samples_in_control = __subset_matrix(parameters,
                                                                                  treatment_group,
                                                                                  control_group)
        matrix_subset = matrix_subset.round(decimals=0)
        prepped_matrix_subset = __filter_matrix_on_cpm_threshold(
            matrix_subset, cpm_genecount_min_threshold=2)  # the bottom read count threshold could be adjusted here

        # just to be sure, reorder the columns in prepped matrix subset
        prepped_matrix_subset = prepped_matrix_subset[[*samples_in_treatment, *samples_in_control]]

        deseq_results, deseq_dds = __perform_analysis_with_deseq(prepped_matrix_subset,
                                                                            samples_in_treatment,
                                                                            samples_in_control,
                                                                            treatment_group,
                                                                            control_group)
        deseq_results["gene_annotation"] = __annotate_gene_names(parameters,
                                                                 deseq_results.index.values)

        edgeR_results = __perform_analysis_with_edger(prepped_matrix_subset,
                                                      samples_in_treatment,
                                                      samples_in_control,
                                                      treatment_group,
                                                      control_group)
        edgeR_results["gene_annotation"] = __annotate_gene_names(parameters,
                                                                 edgeR_results.index.values)

        print(f"Filtering the acquired results with parameters")
        filtered_deseq_results, padj_deseq_results = __filter_results(deseq_results,
                                                                      parameters.padj_alpha,
                                                                      parameters.fold_change_threshold)
        filtered_edger_results, padj_edger_results = __filter_results(edgeR_results,
                                                                      parameters.padj_alpha,
                                                                      parameters.fold_change_threshold)

        # write results
        sample_pair_ident = f"{treatment_group}__vs__{control_group}"
        dir_path = os.path.join(parameters.output_dir, sample_pair_ident)
        os.makedirs(dir_path)
        files = ["full_dge",
                 f"padj={parameters.padj_alpha}_filtered_dge",
                 f"fc={parameters.fold_change_threshold}_padj={parameters.padj_alpha}_filtered_dge"]
        __write_results_for_dataset([deseq_results, padj_deseq_results, filtered_deseq_results],
                                    [f"deseq_{item}" for item in files],
                                    dir_path)
        __write_results_for_dataset([edgeR_results, padj_edger_results, filtered_edger_results],
                                    [f"edger_{item}" for item in files],
                                    dir_path)

        # return stuff
        all_deseq_results = [deseq_dds,
                             deseq_results,
                             # padj_deseq_results,
                             # filtered_deseq_results
                             ]
        all_edger_results = [edgeR_results,
                             # padj_edger_results,
                             # filtered_edger_results
                             ]
        full_results.append([treatment_group, control_group, all_deseq_results, all_edger_results])
    print("DGE analysis is done.")
    return full_results



def __annotate_gene_names(parameters, gene_col):
    # database_for_annot = importr(parameters.organism_info["database"])
    # annotation_dbi.mapIds(database_for_annot)
    if parameters.gene_annotation_resource is None:
        return gene_col
    db = parameters.organism_info["database"]
    mapping_func = ro.r('''
    function(gene_col) {
        library(AnnotationDbi)
        library(''' + db + ''')
        mapped = mapIds(''' + db + ''',
                        keys=gene_col,
                        column=\"SYMBOL\",
                        keytype=\"''' + parameters.gene_annotation_resource + ''''\",
                        multiVals=\"first\",
        )
        return(mapped)
    } 
    ''')
    mapped = mapping_func(gene_col)

    return mapped


def __perform_analysis_with_deseq(prepped_matrix_subset, samples_in_treatment, samples_in_control,
                                  treatment_name, control_name,
                                  ):
    # tutorial says counts in matrix should NOT be normalized
    conditions = pd.DataFrame({"conditions": [*[treatment_name for _ in range(len(samples_in_treatment))],
                                              *[control_name for _ in range(len(samples_in_control))]],
                               },
                              index=[*samples_in_treatment, *samples_in_control]
                              )

    # do the analysis in R
    processing_func = ro.r('''
    function(rnaseqMatrix, conditions) {
      library(DESeq2)
      ddsData <- DESeqDataSetFromMatrix(countData = rnaseqMatrix,
                              colData = conditions,
                              design = ~ conditions)
      dds = DESeq(ddsData)
      return(dds)
    }
    ''')
    rnaseqMatrix = pandas2ri.py2rpy(prepped_matrix_subset)
    colData = pandas2ri.py2rpy(conditions)

    dds = processing_func(rnaseqMatrix, colData)
    results_dds = ro.r('''
    function(dds, treatment, control) {
        output = results(dds, c("conditions", treatment, control)) 
        return(as.data.frame(output))
    }
    ''')(dds, treatment_name, control_name)
    results_dds = pandas2ri.rpy2py(results_dds)

    counts = pd.DataFrame(ro.r('counts')(dds, normalized=True),
                          columns=[*samples_in_treatment, *samples_in_control],
                          index=prepped_matrix_subset.index)

    results_dds["baseMean_treatment"] = counts[samples_in_treatment].T.mean()
    results_dds['baseMean_control'] = counts[samples_in_control].T.mean()
    results_dds['padj'] = results_dds['padj'].fillna(1)

    return results_dds.sort_values(by="pvalue", ascending=True), dds


def __perform_analysis_with_edger(prepped_matrix_subset, samples_in_treatment, samples_in_control,
                                  treatment_name, control_name,
                                  ):
    # conditions = [*[treatment_name for _ in range(len(samples_in_treatment))],
    #               *[control_name for _ in range(len(samples_in_control))]]
    rnaseqMatrix = pandas2ri.py2rpy(prepped_matrix_subset)
    processing_func = ro.r('''
        function(rnaseqMatrix, treatment, control, treatment_size, control_size) {
            conditions = factor(c(rep(treatment, treatment_size), rep(control, control_size)))
            exp_study = DGEList(counts=rnaseqMatrix, group=conditions)
            exp_study = calcNormFactors(exp_study)
            exp_study = estimateDisp(exp_study)
            et = exactTest(exp_study, pair=c(treatment, control))
            tTags = topTags(et,n=NULL)
            result_table = data.frame(tTags$table)
            return(result_table)
        }
        ''')
    results_edger = processing_func(rnaseqMatrix,
                                    treatment_name, control_name,
                                    len(samples_in_treatment), len(samples_in_control))
    results = pandas2ri.rpy2py(results_edger)
    results['logFC'] = results['logFC'] * -1
    # this is already ordered
    # unify the column names in both dataframes
    # edger: logFC     logCPM        PValue           FDR, geneID in index
    # deseq: 'baseMean', 'log2FoldChange', 'lfcSE', 'stat', 'pvalue', 'padj',
    #        'baseMean_treatment', 'baseMean_control'
    # change:   logFC -> log2FoldChange
    #           PValue -> pvalue
    #           FDR -> padj
    results = results.rename(columns={
        "logFC": "log2FoldChange",
        "PValue": "pvalue",
        "FDR": "padj"
    })
    return results


def __filter_results(results_df, padj_alpha, logfc_threshold):
    padj_mask = results_df["pvalue"] < padj_alpha
    logfc_mask = results_df["log2FoldChange"] > logfc_threshold
    mask = padj_mask & logfc_mask
    return results_df[mask], results_df[padj_mask]


def __write_results_for_dataset(result_dataframe_lst, tags_lst, dir_path):
    for df, tag in zip(result_dataframe_lst, tags_lst):
        file_path = os.path.join(dir_path, tag + ".tsv")
        df.to_csv(file_path, sep='\t')
