from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response
import pandas as pd
import os
import re
import json
import unicodedata
from pathlib import Path

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
):
    """
    Recherche un produit par mot-clé dans la description ou la marque.
    Utilisé par l'agent ElevenLabs pour répondre aux questions clients.
    """
    try:
        df = load_data()
    except DataSourceError as exc:
        return json_response({"found": False, "message": str(exc)})

    # Recherche mot par mot pour plus de tolérance
    terms = [re.escape(t) for t in q.strip().split() if t]
    mask = pd.Series([True] * len(df), index=df.index)
    for term in terms:
        mask &= (
            df["description"].str.contains(term, case=False, na=False)
            | df["marque"].str.contains(term, case=False, na=False)
        )
    results = df[mask]

    # Si 0 résultat avec tous les mots, essayer avec le premier mot seul
    if results.empty and terms:
        fallback = re.escape(q.strip().split()[0])
        mask2 = (
            df["description"].str.contains(fallback, case=False, na=False)
            | df["marque"].str.contains(fallback, case=False, na=False)
        )
        results = df[mask2]

    if en_stock_seulement:
        results = results[results["stock"] > 4]

    if results.empty:
        return json_response({"found": False, "message": f"Aucun article trouvé pour '{q}'."})

    items = []
    for _, row in results.head(10).iterrows():
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

    return json_response({"found": True, "count": len(items), "produits": items})


@app.get("/stock/{description}")
def get_stock(description: str):
    """
    Retourne le stock et le prix d'un article précis (correspondance exacte ou proche).
    """
    try:
        df = load_data()
    except DataSourceError as exc:
        return json_response({"found": False, "message": str(exc)})

    # Recherche mot par mot
    terms = [re.escape(t) for t in description.strip().split() if t]
    mask = pd.Series([True] * len(df), index=df.index)
    for term in terms:
        mask &= df["description"].str.contains(term, case=False, na=False)
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
