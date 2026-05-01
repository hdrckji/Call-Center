from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import pandas as pd
import os
import re

app = FastAPI(title="Famiflora Stock API")

EXCEL_PATH = os.path.join(os.path.dirname(__file__), "data", "Test Db Bougies.xlsx")


class DataSourceError(Exception):
    pass

def load_data() -> pd.DataFrame:
    if not os.path.exists(EXCEL_PATH):
        raise DataSourceError(
            "Fichier Excel introuvable. Placez 'Test Db Bougies.xlsx' dans le dossier data/."
        )

    df = pd.read_excel(
        EXCEL_PATH,
        usecols="B,E,K,AG",
        header=0,
        engine="openpyxl",
    )
    df.columns = ["marque", "description", "stock", "prix_vente"]
    # Nettoyer les lignes vides
    df = df.dropna(subset=["description"])
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
        return JSONResponse(content={"found": False, "message": str(exc)}, status_code=503)

    pattern = re.escape(q.strip())
    mask = (
        df["description"].str.contains(pattern, case=False, na=False)
        | df["marque"].str.contains(pattern, case=False, na=False)
    )
    results = df[mask]

    if en_stock_seulement:
        results = results[results["stock"] > 0]

    if results.empty:
        return JSONResponse(
            content={"found": False, "message": f"Aucun article trouvé pour '{q}'."},
            status_code=200,
        )

    items = []
    for _, row in results.iterrows():
        prix = f"{row['prix_vente']:.2f} €" if pd.notna(row["prix_vente"]) else "Prix non disponible"
        stock_label = str(int(row["stock"])) if row["stock"] > 0 else "Rupture de stock"
        items.append({
            "marque": str(row["marque"]) if pd.notna(row["marque"]) else "",
            "description": str(row["description"]),
            "stock": stock_label,
            "prix_vente": prix,
        })

    return {"found": True, "count": len(items), "produits": items}


@app.get("/stock/{description}")
def get_stock(description: str):
    """
    Retourne le stock et le prix d'un article précis (correspondance exacte ou proche).
    """
    try:
        df = load_data()
    except DataSourceError as exc:
        return JSONResponse(content={"found": False, "message": str(exc)}, status_code=503)
    pattern = re.escape(description.strip())
    mask = df["description"].str.contains(pattern, case=False, na=False)
    results = df[mask]

    if results.empty:
        return JSONResponse(
            content={"found": False, "message": f"Article '{description}' introuvable."},
            status_code=200,
        )

    row = results.iloc[0]
    prix = f"{row['prix_vente']:.2f} €" if pd.notna(row["prix_vente"]) else "Prix non disponible"
    return {
        "found": True,
        "marque": str(row["marque"]) if pd.notna(row["marque"]) else "",
        "description": str(row["description"]),
        "stock": int(row["stock"]),
        "en_stock": row["stock"] > 0,
        "prix_vente": prix,
    }
