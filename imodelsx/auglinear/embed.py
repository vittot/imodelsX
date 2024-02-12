from os.path import join as oj
from datasets import Dataset
from tqdm import tqdm
import torch
import numpy as np
from torch.utils.data import DataLoader
import imodelsx.util
from typing import List


def get_model(checkpoint):
    from transformers import BertModel, DistilBertModel
    from transformers import AutoModelForCausalLM
    if "distilbert" in checkpoint.lower():
        model = DistilBertModel.from_pretrained(checkpoint)
    elif "bert-base" in checkpoint.lower() or "BERT" in checkpoint:
        model = BertModel.from_pretrained(checkpoint)
    elif "gpt" in checkpoint.lower():
        model = AutoModelForCausalLM.from_pretrained(
            checkpoint, output_hidden_states=True
        )
    try:
        model = model.cuda()
    except:
        pass
    return model


def embed_and_sum_function(
    example,
    model,
    ngrams: int,
    tokenizer_embeddings,
    tokenizer_ngrams,
    checkpoint: str,
    dataset_key_text: str = None,
    layer: str = "last_hidden_state",
    padding: str = "max_length",
    batch_size: int = 8,
    parsing: str = "",
    nlp_chunks=None,
    all_ngrams: bool = False,
    fit_with_ngram_decomposition: bool = True,
    instructor_prompt: str = "Represent the short phrase for sentiment classification: ",
    sum_embeddings=True,
    prune_stopwords=False,
):
    """Get summed embeddings for a single example

    Params
    ------
    ngrams: int
        What order of ngrams to use (1 for unigrams, 2 for bigrams, ...)
    dataset_key_text:
        str that identifies where data examples are stored, e.g. "sentence" for sst2
    tokenizer_embeddings
        tokenizing for the embedding model
    tokenizer_ngrams
        tokenizing the ngrams (word-based tokenization is more interpretable)
    layer: str
            which layer to extract embeddings from
    batch_size: int
            batch size for simultaneously running ngrams (for a single example)
    parsing: str
        whether to use parsing rather than extracting all ngrams
    nlp_chunks
        if parsing is not empty string, a parser that extracts specific ngrams
    fit_with_ngram_decomposition
        whether to fit the model with ngram decomposition (if not just use the standard sentence)
    instructor_prompt: str
        if using instructor, the prompt to use
    all_ngrams: bool
        whether to include all ngrams of lower order
    """

    # convert to list of strings
    seqs = _get_seqs(
        example, dataset_key_text, fit_with_ngram_decomposition,
        ngrams, tokenizer_ngrams, parsing, nlp_chunks, all_ngrams, prune_stopwords)

    if not checkpoint.startswith("hkunlp/instructor") and (
        not hasattr(tokenizer_embeddings, "pad_token")
        or tokenizer_embeddings.pad_token is None
    ):
        tokenizer_embeddings.pad_token = tokenizer_embeddings.eos_token

    # compute embeddings
    embs = []
    if checkpoint.startswith("hkunlp/instructor"):
        embs = model.encode(
            [[instructor_prompt, x_i] for x_i in seqs], batch_size=batch_size
        )
    else:
        tokens = tokenizer_embeddings(
            seqs, padding=padding, truncation=True, return_tensors="pt"
        )

        ds = Dataset.from_dict(tokens).with_format("torch")

        for batch in DataLoader(ds, batch_size=batch_size, shuffle=False):
            batch = {k: v.to(model.device) for k, v in batch.items()}

            with torch.no_grad():
                output = model(**batch)
            torch.cuda.empty_cache()

            if layer == "pooler_output":
                emb = output["pooler_output"]
            elif layer == "last_hidden_state_mean" or layer == "last_hidden_state":
                # extract (batch_size, seq_len, hidden_size)
                emb = output["last_hidden_state"]
            # convert to (batch_size, hidden_size)
                emb = emb.mean(axis=1)
            elif "hidden_states" in output.keys():
                # extract (layer x (batch_size, seq_len, hidden_size))
                h = output["hidden_states"]

                # convert to (batch_size, seq_len, hidden_size)
                emb = h[0]

                # convert to (batch_size, hidden_size)
                emb = emb.mean(axis=1)
            else:
                raise Exception(f"keys: {output.keys()}")

            embs.append(emb.cpu().detach().numpy())

        embs = np.concatenate(embs)

    # else:
        # raise Exception(f"Unknown model checkpoint {checkpoint}")

    # sum over the embeddings
    if sum_embeddings:
        embs = embs.sum(axis=0).reshape(1, -1)
    if len(seqs) == 0:
        embs *= 0

    return {"embs": embs, "seq_len": len(seqs)}


def _get_seqs(
        example, dataset_key_text, fit_with_ngram_decomposition,
        ngrams, tokenizer_ngrams, parsing, nlp_chunks, all_ngrams, prune_stopwords) -> List[str]:

    if dataset_key_text is not None:
        sentence = example[dataset_key_text]
    else:
        sentence = example

    if fit_with_ngram_decomposition:
        seqs = imodelsx.util.generate_ngrams_list(
            sentence,
            ngrams=ngrams,
            tokenizer_ngrams=tokenizer_ngrams,
            parsing=parsing,
            nlp_chunks=nlp_chunks,
            all_ngrams=all_ngrams,
            prune_stopwords=prune_stopwords,
        )
    elif isinstance(sentence, list):
        seqs = sentence
    elif isinstance(sentence, str):
        seqs = [sentence]
    else:
        raise ValueError("sentence must be a string or list of strings")

    seq_len = len(seqs)
    if seq_len == 0:
        # will multiply embedding by 0 so doesn't matter, but still want to get the shape
        seqs = ["dummy"]
    return seqs


def _clean_np_array(arr):
    """Replace inf and nan with 0"""
    arr[np.isinf(arr)] = 0
    arr[np.isnan(arr)] = 0
    return arr
