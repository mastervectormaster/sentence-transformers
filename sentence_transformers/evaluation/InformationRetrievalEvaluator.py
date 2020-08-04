from . import SentenceEvaluator, SimilarityFunction
import torch
from torch.utils.data import DataLoader
import logging
from tqdm import tqdm
from sentence_transformers.util import batch_to_device, pytorch_cos_sim
import os
import csv
import numpy as np
from typing import List, Tuple, Dict, Set
from collections import defaultdict
import queue

class InformationRetrievalEvaluator(SentenceEvaluator):
    """
    This class evaluates an Information Retrieval (IR) setting.

    Given a set of queries and a large corpus set. It will retrieve for each query the top-k most similar document. It measures
    Mean Reciprocal Rank (MRR), Recall@k, and Normalized Discounted Cumulative Gain (NDCG)
    """

    def __init__(self,
                 queries: Dict[str, str],  #qid => query
                 corpus: Dict[str, str],  #cid => doc
                 relevant_docs: Dict[str, Set[str]],  #qid => Set[cid]
                 query_chunk_size: int = 1000,
                 corpus_chunk_size: int = 500000,
                 mrr_at_k: List[int] = [10],
                 ndcg_at_k: List[int] = [10],
                 accuracy_at_k: List[int] = [1, 3, 5, 10],
                 precision_recall_at_k: List[int] = [1, 3, 5, 10],
                 show_progress_bar: bool = False,
                 batch_size: int = 16,
                 name: str = ''):

        self.queries_ids = []
        for qid in queries:
            if qid in relevant_docs and len(relevant_docs[qid]) > 0:
                self.queries_ids.append(qid)

        self.queries = [queries[qid] for qid in self.queries_ids]

        self.corpus_ids = list(corpus.keys())
        self.corpus = [corpus[cid] for cid in self.corpus_ids]

        self.relevant_docs = relevant_docs
        self.query_chunk_size = query_chunk_size
        self.corpus_chunk_size = corpus_chunk_size
        self.mrr_at_k = mrr_at_k
        self.ndcg_at_k = ndcg_at_k
        self.accuracy_at_k = accuracy_at_k
        self.precision_recall_at_k = precision_recall_at_k
        self.show_progress_bar = show_progress_bar,
        self.batch_size = batch_size
        self.name = name

        if name:
            name = "_" + name

        self.csv_file: str = "Information-Retrieval_evaluation" + name + "_results.csv"
        self.csv_headers = ["epoch", "steps"]


        for k in accuracy_at_k:
            self.csv_headers.append("Accuracy@{}".format(k))

        for k in precision_recall_at_k:
            self.csv_headers.append("Precision@{}".format(k))
            self.csv_headers.append("Recall@{}".format(k))

        for k in mrr_at_k:
            self.csv_headers.append("MRR@{}".format(k))

        for k in ndcg_at_k:
            self.csv_headers.append("NDCG@{}".format(k))

    def __call__(self, model, output_path: str = None, epoch: int = -1, steps: int = -1) -> float:
        if epoch != -1:
            out_txt = " after epoch {}:".format(epoch) if steps == -1 else " in epoch {} after {} steps:".format(epoch, steps)
        else:
            out_txt = ":"

        logging.info("Information Retrieval Evaluation on " + self.name + " dataset" + out_txt)

        max_k = max(max(self.mrr_at_k), max(self.ndcg_at_k), max(self.accuracy_at_k), max(self.precision_recall_at_k))

        # Compute embedding for the queries
        query_embeddings = model.encode(self.queries, show_progress_bar=self.show_progress_bar, batch_size=self.batch_size, convert_to_tensor=True)


        #Init score computation values
        num_hits_at_k = {k: 0 for k in self.accuracy_at_k}

        precisions_at_k = {k: [] for k in self.precision_recall_at_k}
        recall_at_k = {k: [] for k in self.precision_recall_at_k}
        MRR = {k: 0 for k in self.mrr_at_k}
        ndcg = {k: [] for k in self.ndcg_at_k}

        #Compute embedding for the corpus
        corpus_embeddings = model.encode(self.corpus, show_progress_bar=self.show_progress_bar, batch_size=self.batch_size, convert_to_tensor=True)

        for query_start_idx in range(0, len(query_embeddings), self.query_chunk_size):
            query_end_idx = min(query_start_idx + self.query_chunk_size, len(query_embeddings))

            queries_result_list = [[] for _ in range(query_start_idx, query_end_idx)]

            #Iterate over chunks of the corpus
            for corpus_start_idx in range(0, len(corpus_embeddings), self.corpus_chunk_size):
                corpus_end_idx = min(corpus_start_idx + self.corpus_chunk_size, len(corpus_embeddings))

                #Compute cosine similarites
                cos_scores = pytorch_cos_sim(query_embeddings[query_start_idx:query_end_idx], corpus_embeddings[corpus_start_idx:corpus_end_idx]).cpu().numpy()
                cos_scores = np.nan_to_num(cos_scores)

                #Partial sort scores
                cos_score_argpartition = np.argpartition(-cos_scores, max_k)[:, 0:max_k]


                for query_itr in range(len(cos_scores)):
                    for sub_corpus_id in cos_score_argpartition[query_itr]:
                        corpus_id = self.corpus_ids[corpus_start_idx+sub_corpus_id]
                        score = cos_scores[query_itr][sub_corpus_id]
                        queries_result_list[query_itr].append({'corpus_id': corpus_id, 'score': score})

            for query_itr in range(len(queries_result_list)):
                query_id = self.queries_ids[query_start_idx + query_itr]

                #Sort scores
                top_hits = sorted(queries_result_list[query_itr], key=lambda x: x['score'], reverse=True)
                query_relevant_docs = self.relevant_docs[query_id]


                #Accuracy@k - We count the result correct, if at least one relevant doc is accross the top-k documents
                for k_val in self.accuracy_at_k:
                    for hit in top_hits[0:k_val]:
                        if hit['corpus_id'] in query_relevant_docs:
                            num_hits_at_k[k_val] += 1
                            break

                #Precision and Recall@k
                for k_val in self.precision_recall_at_k:
                    num_correct = 0

                    for hit in top_hits[0:k_val]:
                        if hit['corpus_id'] in query_relevant_docs:
                            num_correct += 1

                    precisions_at_k[k_val].append(num_correct / k_val)
                    recall_at_k[k_val].append(num_correct / len(query_relevant_docs))

                #MRR@k
                for k_val in self.mrr_at_k:
                    for rank, hit in enumerate(top_hits[0:k_val]):
                        if hit['corpus_id'] in query_relevant_docs:
                            MRR[k_val] += 1.0 / (rank + 1)
                            break

                #NDCG@k
                for k_val in self.ndcg_at_k:
                    predicted_relevance = [1 if top_hit['corpus_id'] in query_relevant_docs else 0 for top_hit in top_hits[0:k_val]]
                    true_relevances = [1] * len(query_relevant_docs)

                    ndcg_value = self.compute_dcg_at_k(predicted_relevance, k_val) / self.compute_dcg_at_k(true_relevances, k_val)
                    ndcg[k_val].append(ndcg_value)

        #Compute averages
        for k in num_hits_at_k:
            num_hits_at_k[k] /= len(self.queries)

        for k in precisions_at_k:
            precisions_at_k[k] = np.mean(precisions_at_k[k])

        for k in recall_at_k:
            recall_at_k[k] = np.mean(recall_at_k[k])

        for k in ndcg:
            ndcg[k] = np.mean(ndcg[k])

        for k in MRR:
            MRR[k] /= len(self.queries)

        #Output
        logging.info("Queries: {}".format(len(self.queries)))
        logging.info("Corpus: {}".format(len(self.corpus)))

        for k in num_hits_at_k:
            logging.info("Accuracy@{}: {:.2f}%".format(k, num_hits_at_k[k]*100))

        for k in precisions_at_k:
            logging.info("Precision@{}: {:.2f}%".format(k, precisions_at_k[k]*100))

        for k in recall_at_k:
            logging.info("Recall@{}: {:.2f}%".format(k, recall_at_k[k]*100))

        for k in MRR:
            logging.info("MRR@{}: {:.4f}".format(k, MRR[k]))

        for k in ndcg:
            logging.info("NDCG@{}: {:.4f}".format(k, ndcg[k]))

        if output_path is not None:
            csv_path = os.path.join(output_path, self.csv_file)
            if not os.path.isfile(csv_path):
                fOut = open(csv_path, mode="w", encoding="utf-8")
                fOut.write(",".join(self.csv_headers))
                fOut.write("\n")

            else:
                fOut = open(csv_path, mode="a", encoding="utf-8")

            output_data = [epoch, steps]
            for k in self.accuracy_at_k:
                output_data.append(num_hits_at_k[k])

            for k in self.precision_recall_at_k:
                output_data.append(precisions_at_k[k])
                output_data.append(recall_at_k[k])

            for k in self.mrr_at_k:
                output_data.append(MRR[k])

            for k in self.ndcg_at_k:
                output_data.append(ndcg[k])

            fOut.write(",".join(map(str,output_data)))
            fOut.write("\n")
            fOut.close()

        return MRR[max(self.mrr_at_k)]

    @staticmethod
    def compute_dcg_at_k(relevances, k):
        dcg = 0
        for i in range(min(len(relevances), k)):
            dcg += relevances[i] / np.log2(i + 2)  #+2 as we start our idx at 0
        return dcg















