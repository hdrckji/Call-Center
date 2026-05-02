from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response
import pandas as pd
import os
import re
import json
import unicodedata
from pathlib import Path
from difflib import get_close_matches

app = FastAPI(title="Famiflora Stock API")


def clean_text(value) -> str:
    """Nettoie et normalise le texte pour éviter les problèmes d'encodage."""
    if pd.isna(value):
        return ""
    text = str(value)
    # Normaliser en NFC pour corriger les caractères mal encodés
    return unicodedata.normalize("NFC", text)


def json_response(data: dict):
    """Retourne une réponse JSON avec encodage UTF-8 garanti."""
    return Response(
        content=json.dumps(data, ensure_ascii=False),
        media_type="application/json; charset=utf-8",
    )


def stock_status(stock_value: int) -> str:
    """Mappe le stock numérique vers un statut métier lisible par le client."""
    if stock_value > 10:
        return "En stock"
    if stock_value > 4:
        return "Stock tres limite"
    return "Pas en stock"


ALIASES = {
    "alia": "alya",
    "alyah": "alya",
    "bolsus": "bolsius",
    "bonsus": "bolsius",
    "golsus": "bolsius",
}

NOISE_TOKENS = {
    "sans",
    "terminer",
    "encore",
    "avoir",
    "avec",
    "pour",
    "svp",
}


def normalize_for_search(value: str) -> str:
    text = clean_text(value).lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_token(token: str) -> str:
    token = ALIASES.get(token, token)
    if token.endswith("s") and len(token) > 4:
        token = token[:-1]
    return token


def extract_search_vocabulary(df: pd.DataFrame) -> set[str]:
    vocab = set()
    for text in df["search_text"].dropna():
        for token in str(text).split():
            if len(token) >= 3:
                vocab.add(token)
    return vocab


def fuzzy_correct_token(token: str, vocabulary: set[str]) -> str:
    if token in vocabulary or len(token) < 4:
        return token
    match = get_close_matches(token, vocabulary, n=1, cutoff=0.82)
    if match:
        return match[0]
    return token


def tokenize_query(query: str, vocabulary: set[str] | None = None) -> list[str]:
    base_tokens = normalize_for_search(query).split()
    normalized = []
    for token in base_tokens:
        token = normalize_token(token)
        if vocabulary is not None:
            token = fuzzy_correct_token(token, vocabulary)
        if token and token not in NOISE_TOKENS:
            normalized.append(token)
    return normalized

EXCEL_PATH = os.path.join(os.path.dirname(__file__), "data", "Test Db Bougies.xlsx")


class DataSourceError(Exception):
    pass


def resolve_excel_path() -> str:
    file_name = "Test Db Bougies.xlsx"
    base_dir = Path(__file__).resolve().parent
    cwd_dir = Path.cwd()

    candidates = [
        base_dir / "data" / file_name,
        base_dir / file_name,
        cwd_dir / "data" / file_name,
        cwd_dir / file_name,
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    # Fallback: rechercher le fichier dans le projet en cas de root dir Railway différent
    for found in base_dir.rglob(file_name):
        if found.is_file():
            return str(found)

    for found in cwd_dir.rglob(file_name):
        if found.is_file():
            return str(found)

    checked = ", ".join(str(path) for path in candidates)
    raise DataSourceError(
        "Fichier Excel introuvable. Placez 'Test Db Bougies.xlsx' dans le dossier data/. "
        f"Chemins testés: {checked}"
    )

def load_data() -> pd.DataFrame:
    excel_path = resolve_excel_path()

    df = pd.read_excel(
        excel_path,
        usecols="B,E,K,AG",
        header=0,
        engine="openpyxl",
    )
    df.columns = ["marque", "description", "stock", "prix_vente"]
    df["marque"] = df["marque"].apply(clean_text)
    df["description"] = df["description"].apply(clean_text)
    df["search_text"] = (df["marque"] + " " + df["description"]).apply(normalize_for_search)
    df = df[df["description"].str.strip() != ""]
    df["stock"] = pd.to_numeric(df["stock"], errors="coerce").fillna(0).astype(int)
    df["prix_vente"] = pd.to_numeric(df["prix_vente"], errors="coerce")
    return df


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/search")
def search_products(
    q: str = Query(..., description="Terme de recherche (nom, marque, description)"),
    en_stock_seulement: bool = Query(False, description="Filtrer uniquement les articles en stock"),
    limit: int = Query(1, ge=1, le=10, description="Nombre max de resultats retournes"),
):
    """
    Recherche un produit par mot-clé dans la description ou la marque.
    Utilisé par l'agent ElevenLabs pour répondre aux questions clients.
    """
    try:
        df = load_data()
    except DataSourceError as exc:
        return json_response({"found": False, "message": str(exc)})

    vocabulary = extract_search_vocabulary(df)
    # Recherche mot par mot normalisee (accents, pluriels, aliases et fuzzy matching).
    terms = tokenize_query(q, vocabulary)
    mask = pd.Series([True] * len(df), index=df.index)
    for term in terms:
        escaped = re.escape(term)
        mask &= df["search_text"].str.contains(escaped, case=False, na=False)
    results = df[mask]

    # Fallback: si la transcription contient du bruit, garder les lignes avec le plus de tokens reconnus.
    if results.empty and terms:
        score = pd.Series(0, index=df.index)
        for term in terms:
            escaped = re.escape(term)
            score += df["search_text"].str.contains(escaped, case=False, na=False).astype(int)
        best_score = int(score.max())
        if best_score > 0:
            results = df[score == best_score]

    if en_stock_seulement:
        results = results[results["stock"] > 4]

    if results.empty:
        return json_response({"found": False, "message": f"Aucun article trouvé pour '{q}'."})

    total_matches = int(len(results))

    items = []
    for _, row in results.head(limit).iterrows():
        prix = f"{row['prix_vente']:.2f} €" if pd.notna(row["prix_vente"]) else "Prix non disponible"
        stock_label = stock_status(int(row["stock"]))
        items.append({
            "marque": row["marque"],
            "description": row["description"],
            "stock": stock_label,
            "prix_vente": prix,
            "reservation_par_telephone": False,
            "reservation_message": "Il n'est pas possible de reserver un article par telephone.",
        })

    return json_response({
        "found": True,
        "count": total_matches,
        "returned_count": len(items),
        "produits": items,
    })


@app.get("/stock/{description}")
def get_stock(description: str):
    """
    Retourne le stock et le prix d'un article précis (correspondance exacte ou proche).
    """
    try:
        df = load_data()
    except DataSourceError as exc:
        return json_response({"found": False, "message": str(exc)})

    vocabulary = extract_search_vocabulary(df)
    # Recherche mot par mot normalisee (accents, pluriels, aliases et fuzzy matching).
    terms = tokenize_query(description, vocabulary)
    mask = pd.Series([True] * len(df), index=df.index)
    for term in terms:
        escaped = re.escape(term)
        mask &= df["search_text"].str.contains(escaped, case=False, na=False)
    results = df[mask]

    if results.empty:
        return json_response({"found": False, "message": f"Article '{description}' introuvable."})

    row = results.iloc[0]
    prix = f"{row['prix_vente']:.2f} €" if pd.notna(row["prix_vente"]) else "Prix non disponible"
    stock_value = int(row["stock"])
    stock_label = stock_status(stock_value)
    return json_response({
        "found": True,
        "marque": row["marque"],
        "description": row["description"],
        "stock": stock_label,
        "en_stock": stock_value > 4,
        "prix_vente": prix,
        "reservation_par_telephone": False,
        "reservation_message": "Il n'est pas possible de reserver un article par telephone.",
    })
