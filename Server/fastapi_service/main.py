import os
import joblib
import numpy  as np
import pandas as pd
import torch
import torch.nn as nn

from dotenv       import load_dotenv
from fastapi      import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic     import BaseModel
from typing       import Optional, List, Literal
from sqlalchemy   import create_engine, text
from sqlalchemy.engine import URL
# pip install numpy pandas torch joblib python-dotenv fastapi uvicorn sqlalchemy pymysql scikit-learn
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
DEVICE = torch.device("cpu")

load_dotenv()

# =============================================================
#  ✏️  SET YOUR MODEL PATHS HERE
# =============================================================
MODEL_PT_PATH  = "models/demand_model.pth"
SCALER_X_PATH  = "models/scaler_X.pkl"
SCALER_Y_PATH  = "models/scaler_y.pkl"
# =============================================================

app = FastAPI(
    title="Warehouse ML API",
    version="1.0",
    docs_url="/docs",      
    redoc_url="/redoc"     
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# ── DB ────────────────────────────────────────────────────────
DB_URL = URL.create(
    drivername="mysql+pymysql",
    username=os.getenv("DATABASE_USER"),
    password=os.getenv("DATABASE_PASSWORD"),
    host=os.getenv("DATABASE_HOST", "localhost"),
    port=int(os.getenv("DATABASE_PORT", 3306)),
    database=os.getenv("DATABASE_NAME")
)
engine = create_engine(DB_URL, pool_pre_ping=True)

# ── Must match training order exactly ────────────────────────
FEATURE_COLS = [
    "day_of_week", "month", "quarter",
    "is_weekend", "is_month_start", "is_month_end",
    "lag_7", "lag_14", "lag_30", "lag_365",
    "rolling_mean_7", "rolling_mean_30", "rolling_mean_90", "rolling_std_7",
    "trend_direction", "yoy_growth"
]


_model    = None
_scaler_X = None
_scaler_y = None

def load_model():
    global _model, _scaler_X, _scaler_y

    missing = [p for p in [MODEL_PT_PATH, SCALER_X_PATH, SCALER_Y_PATH] if not os.path.exists(p)]
    if missing:
        print(f"⚠️  Model files not found: {missing}")
        return

    _model = SalesANN(input_dim=len(FEATURE_COLS)).to(DEVICE)   
    _model.load_state_dict(torch.load(MODEL_PT_PATH, map_location=DEVICE))
    _model.eval()

    _scaler_X = joblib.load(SCALER_X_PATH)
    _scaler_y = joblib.load(SCALER_Y_PATH)

    print(f"✅ Model loaded on CPU from {MODEL_PT_PATH}")

load_model()
# from typing import List, Literal
# from pydantic import BaseModel

# ============================================================
# Request Schema
# ============================================================

class ItemRequest(BaseModel):
    item_id: str

class BatchRequest(BaseModel):
    items: List[ItemRequest]
    horizon: str = "week"  


# ============================================================
# Endpoint
# ============================================================
@app.post("/predict-batch")
def predict_batch_api(req: BatchRequest):

    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    FEATURES = [
        'day_of_week','month','quarter','is_weekend',
        'is_month_start','is_month_end','lag_7','lag_14',
        'lag_30','lag_365','rolling_mean_7','rolling_mean_30',
        'rolling_mean_90','rolling_std_7',
        'trend_direction','yoy_growth'
    ]

    # ─────────────────────────────────────────────
    # STEP 1: Fetch latest features using ONLY item_id
    input_data = []

    with engine.connect() as conn:
        for item in req.items:
            row = conn.execute(text(f"""
                SELECT {', '.join(FEATURES)}, sale_date
                FROM sales_data
                WHERE item_id = :item_id
                  AND lag_7 IS NOT NULL
                ORDER BY sale_date DESC
                LIMIT 1
            """), {
                "item_id": item.item_id
            }).mappings().fetchone()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"No data for item {item.item_id}"
                )

            feature_dict = {}

            for col in FEATURES:
                val = row[col]

                if val is None:
                    # default fallback values
                    if col in ['rolling_std_7']:
                        val = 0.0
                    elif col in ['yoy_growth', 'trend_direction']:
                        val = 0.0
                    else:
                        val = 0.0   # safe fallback

                feature_dict[col] = float(val)
            input_data.append(feature_dict)

    # ─────────────────────────────────────────────
    def run_one_pass(feature_dicts):
        all_features = np.array([
            [f[feat] for feat in FEATURES]
            for f in feature_dicts
        ], dtype=np.float32)

        scaled = _scaler_X.transform(all_features)
        tensor_in = torch.tensor(scaled, device=DEVICE)

        with torch.no_grad():
            raw = _model(tensor_in)

        results = _scaler_y.inverse_transform(raw.cpu().numpy())
        return [round(float(r[0]), 2) for r in results]

    # ─────────────────────────────────────────────
    def simulate_next_week(feature_dict, weekly_pred):
        f = feature_dict.copy()
        daily_pred = weekly_pred / 7

        f['lag_14'] = f['lag_7']
        f['lag_30'] = (f['lag_30'] * 3 + weekly_pred) / 4
        f['lag_7']  = daily_pred

        f['rolling_mean_7']  = (f['rolling_mean_7'] * 6 + daily_pred) / 7
        f['rolling_mean_30'] = (f['rolling_mean_30'] * 29 + daily_pred) / 30

        f['trend_direction'] = f['lag_7'] - f['lag_30']

        f['day_of_week'] = (int(f['day_of_week']) + 7) % 7
        f['is_weekend']  = 1 if f['day_of_week'] >= 5 else 0

        return f

    # ─────────────────────────────────────────────
    # WEEK prediction
    weekly_preds = run_one_pass(input_data)

    if req.horizon == "week":
        return [
            {
                "item_id": req.items[i].item_id,
                "weekly": weekly_preds[i]
            }
            for i in range(len(input_data))
        ]

    # ─────────────────────────────────────────────
    # MONTH prediction
    monthly_totals   = [0] * len(input_data)
    weekly_breakdown = [[] for _ in range(len(input_data))]
    current_features = [f.copy() for f in input_data]

    for _ in range(4):
        week_preds = run_one_pass(current_features)

        for i, pred in enumerate(week_preds):
            monthly_totals[i] += pred
            weekly_breakdown[i].append(pred)
            current_features[i] = simulate_next_week(current_features[i], pred)

    if req.horizon == "month":
        return [
            {
                "item_id": req.items[i].item_id,
                "monthly": monthly_totals[i],
                "weekly_breakdown": weekly_breakdown[i]
            }
            for i in range(len(input_data))
        ]

    if req.horizon == "both":
        return [
            {
                "item_id": req.items[i].item_id,
                "weekly": weekly_preds[i],
                "monthly": monthly_totals[i],
                "weekly_breakdown": weekly_breakdown[i]
            }
            for i in range(len(input_data))
        ]
        
# =============================================================
#  HEALTH
# =============================================================
@app.get("/")
def home():
    return {
        "status":       "ok",
        "model_loaded": _model is not None,
        "device":       str(DEVICE),
        "model_path":   MODEL_PT_PATH,
    }

# =============================================================
#  PREDICT
# =============================================================
class PredictRequest(BaseModel):
    item_id:  str

@app.post("/predict")
def predict(req: PredictRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT {', '.join(FEATURE_COLS)}, sale_date
            FROM   sales_data
            WHERE  item_id  = :item_id
              AND  lag_7 IS NOT NULL
            ORDER  BY sale_date DESC
            LIMIT  1
        """), {"item_id": req.item_id}).mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No feature data found")

    X = np.array([[row[c] for c in FEATURE_COLS]], dtype=np.float32)
    X_scaled = _scaler_X.transform(X).astype(np.float32)

    tensor_input = torch.tensor(X_scaled, device=DEVICE)

    with torch.no_grad():
        y_scaled = _model(tensor_input).cpu().numpy()

    pred = float(_scaler_y.inverse_transform(y_scaled)[0][0])

    return {
        "store_id": req.store_id,
        "item_id": req.item_id,
        "predicted_sales_next_7": round(max(pred, 0), 2),
        "based_on_date": str(row["sale_date"]),
    }


# =============================================================
#  PREDICT ALL
# =============================================================
@app.get("/predict-all")
def predict_all():
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT store_id, item_id, sale_date, {', '.join(FEATURE_COLS)}
            FROM sales_data s1
            WHERE sale_date = (
                SELECT MAX(sale_date)
                FROM sales_data s2
                WHERE s2.store_id = s1.store_id
                  AND s2.item_id  = s1.item_id
                  AND s2.lag_7 IS NOT NULL
            )
        """)).mappings().all()

    results = []

    for row in rows:
        X = np.array([[row[c] for c in FEATURE_COLS]], dtype=np.float32)
        X_scaled = _scaler_X.transform(X).astype(np.float32)

        tensor_input = torch.tensor(X_scaled, device=DEVICE)

        with torch.no_grad():
            y_scaled = _model(tensor_input).cpu().numpy()

        pred = float(_scaler_y.inverse_transform(y_scaled)[0][0])

        results.append({
            "store_id": row["store_id"],
            "item_id": row["item_id"],
            "predicted_sales_next_7": round(max(pred, 0), 2),
            "based_on_date": str(row["sale_date"]),
        })

    return results