import collections
from typing import Any, Callable, Dict, List, Union, Optional

import numpy as np

from haystack import Pipeline
from haystack.core.component import Component
from haystack.evaluation.eval_utils import get_answers_from_output, preprocess_text
from haystack.evaluation.metrics import Metric, MetricsResult

from haystack.lazy_imports import LazyImport
from haystack.utils import ComponentDevice, expit

with LazyImport(message="Run 'pip install scikit-learn \"sentence-transformers>=2.2.0\"'") as metrics_import:
    from sentence_transformers import SentenceTransformer, CrossEncoder, util
    from transformers import AutoConfig


class EvaluationResult:
    """
    EvaluationResult keeps track of all the information related to evaluation, namely the runnable (Pipeline or
    component), inputs, outputs, and expected outputs.
    The EvaluationResult keeps track of all the information stored by eval.

    :param runnable: The runnable (Pipeline or component) used for evaluation.
    :param inputs: List of inputs used for evaluation.
    :param outputs: List of outputs generated by the runnable.
    :param expected_outputs: List of expected outputs used for evaluation.
    """

    def __init__(
        self,
        runnable: Union[Pipeline, Component],
        inputs: List[Dict[str, Any]],
        outputs: List[Dict[str, Any]],
        expected_outputs: List[Dict[str, Any]],
    ) -> None:
        self.runnable = runnable
        self.inputs = inputs
        self.outputs = outputs
        self.expected_outputs = expected_outputs

        # Determine the type of the runnable
        if str(type(runnable).__name__) == "Pipeline":
            self.runnable_type = "pipeline"
        else:
            self.runnable_type = "component"

        # Mapping of metrics to their corresponding functions.
        # This should be kept in sync with the Metric enum
        self._supported_metrics: Dict[Metric, Callable[..., MetricsResult]] = {
            Metric.RECALL: self._calculate_recall,
            Metric.MRR: self._calculate_mrr,
            Metric.MAP: self._calculate_map,
            Metric.F1: self._calculate_f1,
            Metric.EM: self._calculate_em,
            Metric.SAS: self._calculate_sas,
        }

    def calculate_metrics(self, metric: Union[Metric, Callable[..., MetricsResult]], **kwargs) -> MetricsResult:
        """
        Calculate evaluation metrics based on the provided Metric or using the custom metric function.

        :param metric: The Metric indicating the type of metric to calculate or custom function to compute.
        :return: MetricsResult containing the calculated metric.
        """

        if isinstance(metric, Metric):
            return self._supported_metrics[metric](**kwargs)

        return metric(self, **kwargs)

    def _calculate_recall(self):
        return MetricsResult({"recall": None})

    def _calculate_map(self):
        return MetricsResult({"mean_average_precision": None})

    def _calculate_mrr(self):
        return MetricsResult({"mean_reciprocal_rank": None})

    def _compute_f1_single(self, label_toks: List[str], pred_toks: List[str]) -> float:
        """
        Compute F1 score for a single sample.
        """
        common: collections.Counter = collections.Counter(label_toks) & collections.Counter(pred_toks)
        num_same = sum(common.values())
        if len(label_toks) == 0 or len(pred_toks) == 0:
            # If either is no-answer, then F1 is 1 if they agree, 0 otherwise
            return int(label_toks == pred_toks)
        if num_same == 0:
            return 0
        precision = 1.0 * num_same / len(pred_toks)
        recall = 1.0 * num_same / len(label_toks)
        f1 = (2 * precision * recall) / (precision + recall)
        return f1

    def _calculate_f1(
        self,
        output_key: str,
        regexes_to_ignore: Optional[List[str]] = None,
        ignore_case: bool = False,
        ignore_punctuation: bool = False,
        ignore_numbers: bool = False,
    ) -> MetricsResult:
        """
        Calculates the F1 score between two lists of predictions and labels.
        F1 score measures the word overlap between the predicted text and the corresponding ground truth label.

        :param output_key: The key of the output to use for comparison.
        :param regexes_to_ignore (list, optional): A list of regular expressions. If provided, it removes substrings
            matching these regular expressions from both predictions and labels before comparison. Defaults to None.
        :param ignore_case (bool, optional): If True, performs case-insensitive comparison. Defaults to False.
        :param ignore_punctuation (bool, optional): If True, removes punctuation from both predictions and labels before
            comparison. Defaults to False.
        :param ignore_numbers (bool, optional): If True, removes numerical digits from both predictions and labels
            before comparison. Defaults to False.
        :return: A MetricsResult object containing the calculated F1 score.
        """

        predictions = get_answers_from_output(
            outputs=self.outputs, output_key=output_key, runnable_type=self.runnable_type
        )
        labels = get_answers_from_output(
            outputs=self.expected_outputs, output_key=output_key, runnable_type=self.runnable_type
        )

        if len(predictions) != len(labels):
            raise ValueError("The number of predictions and labels must be the same.")
        if len(predictions) == len(labels) == 0:
            # Return F1 as 0 for no inputs
            return MetricsResult({"f1": 0.0})

        predictions = preprocess_text(predictions, regexes_to_ignore, ignore_case, ignore_punctuation, ignore_numbers)
        labels = preprocess_text(labels, regexes_to_ignore, ignore_case, ignore_punctuation, ignore_numbers)

        # Tokenize by splitting on spaces
        tokenized_predictions = [pred.split() for pred in predictions]
        tokenized_labels = [label.split() for label in labels]

        f1_scores = [
            self._compute_f1_single(label_toks, pred_toks)
            for label_toks, pred_toks in zip(tokenized_labels, tokenized_predictions)
        ]

        f1 = np.mean(f1_scores)

        return MetricsResult({"f1": f1})

    def _calculate_em(
        self,
        output_key: str,
        regexes_to_ignore: Optional[List[str]] = None,
        ignore_case: bool = False,
        ignore_punctuation: bool = False,
        ignore_numbers: bool = False,
    ) -> MetricsResult:
        """
        Calculates the Exact Match (EM) score between two lists of predictions and labels.
        Exact Match (EM) score measures the percentage of samples where the predicted text exactly matches the
          corresponding ground truth label.

        :param output_key: The key of the output to use for comparison.
        :param regexes_to_ignore (list, optional): A list of regular expressions. If provided, it removes substrings
            matching these regular expressions from both predictions and labels before comparison. Defaults to None.
        :param ignore_case (bool, optional): If True, performs case-insensitive comparison. Defaults to False.
        :param ignore_punctuation (bool, optional): If True, removes punctuation from both predictions and labels before
            comparison. Defaults to False.
        :param ignore_numbers (bool, optional): If True, removes numerical digits from both predictions and labels
            before comparison. Defaults to False.
        :return: A MetricsResult object containing the calculated Exact Match (EM) score.
        """

        predictions = get_answers_from_output(
            outputs=self.outputs, output_key=output_key, runnable_type=self.runnable_type
        )
        labels = get_answers_from_output(
            outputs=self.expected_outputs, output_key=output_key, runnable_type=self.runnable_type
        )

        if len(predictions) != len(labels):
            raise ValueError("The number of predictions and labels must be the same.")
        if len(predictions) == len(labels) == 0:
            # Return Exact Match as 0 for no inputs
            return MetricsResult({"exact_match": 0.0})

        predictions = preprocess_text(predictions, regexes_to_ignore, ignore_case, ignore_punctuation, ignore_numbers)
        labels = preprocess_text(labels, regexes_to_ignore, ignore_case, ignore_punctuation, ignore_numbers)

        score_list = np.array(predictions) == np.array(labels)
        exact_match_score = np.mean(score_list)

        return MetricsResult({"exact_match": exact_match_score})

    def _calculate_sas(
        self,
        output_key: str,
        regexes_to_ignore: Optional[List[str]] = None,
        ignore_case: bool = False,
        ignore_punctuation: bool = False,
        ignore_numbers: bool = False,
        model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        batch_size: int = 32,
        device: Optional[ComponentDevice] = None,
        token: Optional[Union[str, bool]] = None,
    ) -> MetricsResult:
        """
        Calculates the Semantic Answer Similarity (SAS) score between two lists of predictions and labels.
        Semantic Answer Similarity (SAS) score measures the Transformer-based similarity between the predicted text and
        the corresponding ground truth label.

        :param output_key: The key of the output to use for comparison.
        :param regexes_to_ignore (list, optional): A list of regular expressions. If provided, it removes substrings
            matching these regular expressions from both predictions and labels before comparison. Defaults to None.
        :param ignore_case (bool, optional): If True, performs case-insensitive comparison. Defaults to False.
        :param ignore_punctuation (bool, optional): If True, removes punctuation from both predictions and labels before
            comparison. Defaults to False.
        :param ignore_numbers (bool, optional): If True, removes numerical digits from both predictions and labels
            before comparison. Defaults to False.
        :param model: SentenceTransformers semantic textual similarity model, should be path or string pointing to
            a downloadable model.
        :param batch_size: Number of prediction-label pairs to encode at once.
        :param device: The device on which the model is loaded. If `None`, the default device is automatically
            selected.
        :param token: The token to use as HTTP bearer authorization for private models from Huggingface.
            If True, will use the token generated when running huggingface-cli login (stored in ~/.huggingface).
            Additional information can be found here:
            https://huggingface.co/transformers/main_classes/model.html#transformers.PreTrainedModel.from_pretrained
        :return: A MetricsResult object containing the calculated Semantic Answer Similarity (SAS) score and the
            list of similarity scores obtained for each prediction-label pair.
        """
        metrics_import.check()

        predictions = get_answers_from_output(
            outputs=self.outputs, output_key=output_key, runnable_type=self.runnable_type
        )
        labels = get_answers_from_output(
            outputs=self.expected_outputs, output_key=output_key, runnable_type=self.runnable_type
        )

        if len(predictions) != len(labels):
            raise ValueError("The number of predictions and labels must be the same.")
        if len(predictions) == len(labels) == 0:
            # Return SAS as 0 for no inputs
            return MetricsResult({"sas": 0.0, "scores": [0.0]})

        predictions = preprocess_text(predictions, regexes_to_ignore, ignore_case, ignore_punctuation, ignore_numbers)
        labels = preprocess_text(labels, regexes_to_ignore, ignore_case, ignore_punctuation, ignore_numbers)

        config = AutoConfig.from_pretrained(model, use_auth_token=token)
        cross_encoder_used = False
        if config.architectures:
            cross_encoder_used = any(arch.endswith("ForSequenceClassification") for arch in config.architectures)

        device = ComponentDevice.resolve_device(device)

        # Based on the Model string we can load either Bi-Encoders or Cross Encoders.
        # Similarity computation changes for both approaches

        if cross_encoder_used:
            # For Cross Encoders we create a list of pairs of predictions and labels
            similarity_model = CrossEncoder(
                model,
                device=device.to_torch_str(),
                tokenizer_args={"use_auth_token": token},
                automodel_args={"use_auth_token": token},
            )
            sentence_pairs = [[pred, label] for pred, label in zip(predictions, labels)]
            similarity_scores = similarity_model.predict(sentence_pairs, batch_size=batch_size, convert_to_numpy=True)

            # All Cross Encoders do not return a set of logits scores that are normalized
            # We normalize scores if they are larger than 1
            if (similarity_scores > 1).any():
                similarity_scores = expit(similarity_scores)

            # Convert scores to list of floats from numpy array
            similarity_scores = similarity_scores.tolist()

        else:
            # For Bi-encoders we create embeddings separately for predictions and labels
            similarity_model = SentenceTransformer(model, device=device.to_torch_str(), use_auth_token=token)
            pred_embeddings = similarity_model.encode(predictions, batch_size=batch_size, convert_to_tensor=True)
            label_embeddings = similarity_model.encode(labels, batch_size=batch_size, convert_to_tensor=True)

            # Compute cosine-similarities
            scores = util.cos_sim(pred_embeddings, label_embeddings)

            # cos_sim computes cosine similarity between all pairs of vectors in pred_embeddings and label_embeddings
            # It returns a matrix with shape (len(predictions), len(labels))
            similarity_scores = [scores[i][i].item() for i in range(len(predictions))]

        sas_score = np.mean(similarity_scores)

        return MetricsResult({"sas": sas_score, "scores": similarity_scores})


def eval(
    runnable: Union[Pipeline, Component], inputs: List[Dict[str, Any]], expected_outputs: List[Dict[str, Any]]
) -> EvaluationResult:
    """
    Evaluates the provided Pipeline or component based on the given inputs and expected outputs.

    This function facilitates the evaluation of a given runnable (either a Pipeline or a component) using the provided
    inputs and corresponding expected outputs.

    :param runnable: The runnable (Pipeline or component) used for evaluation.
    :param inputs: List of inputs used for evaluation.
    :param expected_outputs: List of expected outputs used for evaluation.

    :return: An instance of EvaluationResult containing information about the evaluation, including the runnable,
    inputs, outputs, and expected outputs.
    """

    outputs = []

    # Check that expected outputs has the correct shape
    if len(inputs) != len(expected_outputs):
        raise ValueError(
            f"The number of inputs ({len(inputs)}) does not match the number of expected outputs "
            f"({len(expected_outputs)}). Please ensure that each input has a corresponding expected output."
        )

    for input_ in inputs:
        output = runnable.run(input_)
        outputs.append(output)

    return EvaluationResult(runnable, inputs, outputs, expected_outputs)