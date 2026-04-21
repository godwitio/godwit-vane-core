from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import ComplementNB
from sklearn.pipeline import Pipeline


def build_pipeline(n_samples: int) -> Pipeline:
    if n_samples < 100:
        min_df = 1
    elif n_samples < 300:
        min_df = 2
    else:
        min_df = 3

    return Pipeline([
        ("tfidf", TfidfVectorizer(
            ngram_range  = (1, 2),
            sublinear_tf = True,
            min_df       = min_df,
        )),
        ("nb", ComplementNB(alpha=0.3)),
    ])
