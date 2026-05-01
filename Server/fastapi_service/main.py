import os
import joblib
import numpy  as np
import pandas as pd
import xgboost as xgb

from dotenv           import load_dotenv
from fastapi          import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy       import create_engine, text
from sqlalchemy.engine import URL

load_dotenv()

MODEL_PATH  = "./models/xgbmodel.pkl"

FEATURES = [
    'day_of_week','month','quarter','is_weekend',
    'is_month_start','is_month_end','lag_7','lag_14',
    'lag_30','lag_365','rolling_mean_7','rolling_mean_30',
    'rolling_mean_90','rolling_std_7','trend_direction','yoy_growth'
]

DB_URL = URL.create(
    drivername="mysql+pymysql",
    username=os.getenv("DATABASE_USER"),
    password=os.getenv("DATABASE_PASSWORD"),
    host=os.getenv("DATABASE_HOST", "localhost"),
    port=int(os.getenv("DATABASE_PORT", 3306)),
    database=os.getenv("DATABASE_NAME")
)
engine = create_engine(DB_URL, pool_pre_ping=True)

_model  = None
_scaler = None

def load_model():
    global _model
    if not os.path.exists(MODEL_PATH):
        print(f"Model file not found: {MODEL_PATH}")
        return
    _model = joblib.load(MODEL_PATH)
    print(f"XGBoost model loaded from {MODEL_PATH}")
load_model()

app = FastAPI(title="Warehouse ML API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# m = joblib.load("./models/xgbmodel.pkl")
# print(type(m)) 
def load_data():
    Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = [], [], [], [], [], []
    with engine.connect() as conn:
        Products     = [dict(r) for r in conn.execute(text("SELECT * FROM products")).mappings().all()]
        Boxes        = [dict(r) for r in conn.execute(text("SELECT * FROM boxes")).mappings().all()]
        Shelves      = [dict(r) for r in conn.execute(text("SELECT * FROM shelves")).mappings().all()]
        Racks        = [dict(r) for r in conn.execute(text("SELECT * FROM racks")).mappings().all()]
        RawMaterials = [dict(r) for r in conn.execute(text("SELECT * FROM raw_materials")).mappings().all()]
        Salesdata    = [dict(r) for r in conn.execute(text("SELECT * FROM sales_data")).mappings().all()]
    return Products, Boxes, Shelves, Racks, RawMaterials, Salesdata

@app.post("/optimizelayout")
def optimize_layout():
    try:
        print("Milestone 1: Starting layout optimization process...")

        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        try:
            Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()
            print(f"Loaded: {len(Racks)} racks, {len(Shelves)} shelves, {len(Boxes)} boxes, {len(Salesdata)} sales rows.")
        except Exception as e:
            print(f"Error loading warehouse data: {e}")
            raise HTTPException(status_code=500, detail="Failed to load warehouse data")

        shelf_map = {str(s.get('id')): s for s in Shelves}
        rack_map  = {str(r.get('id')): r for r in Racks}

        enriched_boxes = []
        for box in Boxes:
            shelf_id  = str(box.get('shelf_id', ''))
            shelf     = shelf_map.get(shelf_id, {})
            rack_id   = str(shelf.get('rack_id', ''))
            rack      = rack_map.get(rack_id, {})

            enriched_boxes.append({
                'box_id': str(box.get('id')),
                'rack_code': str(rack.get('rack_code') or '').zfill(2),
                'shelf_code': str(shelf.get('shelf_code') or '').zfill(2),
                'box_code': str(box.get('box_code') or '').zfill(2),
                'rack_order': int(rack.get('position') or rack.get('rack_number') or rack.get('id') or 0),
                'shelf_order': int(shelf.get('position') or shelf.get('shelf_number') or shelf.get('id') or 0),
                'box_order': int(box.get('position') or box.get('box_number') or box.get('id') or 0),
            })

        enriched_boxes.sort(key=lambda b: (b['rack_order'], b['shelf_order'], b['box_order']))

        if not enriched_boxes:
            raise HTTPException(status_code=500, detail="No boxes found in DB")

        product_current_loc = {}
        for product in Products:
            pid    = str(product.get('id') or product.get('item_id'))
            box_id = str(product.get('box_id', ''))
            if box_id and box_id != 'None':
                box = next((b for b in enriched_boxes if b['box_id'] == box_id), None)
                if box:
                    rack_code  = f"R{str(box['rack_code']).zfill(2)}"
                    shelf_code = f"{rack_code}-SH{str(box['shelf_code']).zfill(2)}"
                    box_code   = f"B{str(box['box_code']).zfill(2)}"
                    product_current_loc[pid] = f"{rack_code} {shelf_code} {box_code}"
                else:
                    product_current_loc[pid] = 'Unassigned'
            else:
                product_current_loc[pid] = 'Unassigned'

        sales_lookup = {}
        try:
            df = pd.DataFrame(Salesdata)
            df['sale_date'] = pd.to_datetime(df['sale_date'])
            df['item_id']   = df['item_id'].astype(str)
            for col in FEATURES:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
            df = df[df['lag_7'].notna()]
            latest_df = df.sort_values('sale_date').groupby('item_id').last().reset_index()
            for _, row in latest_df.iterrows():
                sales_lookup[row['item_id']] = {
                    col: float(row[col]) if col in row else 0.0
                    for col in FEATURES
                }
            print(f"Sales lookup built ({len(sales_lookup)} items).")
        except Exception as e:
            print(f"Error building sales lookup: {e}")

        results = []
        for product in Products:
            try:
                item_id = str(product.get('id') or product.get('item_id'))
                if item_id not in sales_lookup:
                    continue

                name  = product.get('name', f'Item {item_id}')
                cat   = product.get('category', 'Electronics')
                stock = float(product.get('current_stock', 0))
                c_loc = product_current_loc.get(item_id, 'Unassigned')

                feat_dict = sales_lookup[item_id]
                X = np.array([[feat_dict[f] for f in FEATURES]], dtype=np.float32)
                if _scaler:
                    X = _scaler.transform(X)

                weekly_demand = round(float(_model.predict(X)[0]))
                daily_rate    = weekly_demand / 7
                days_left     = round(stock / max(daily_rate, 0.1))
                trend_val     = feat_dict['trend_direction']

                if days_left <= 3:
                    risk = 'CRITICAL'
                elif days_left <= 7:
                    risk = 'HIGH'
                elif days_left <= 14:
                    risk = 'MEDIUM'
                else:
                    risk = 'LOW'

                demand_score   = min(100, (weekly_demand / 500) * 100)
                stockout_score = (100 if days_left <= 3 else 80 if days_left <= 7 else 50 if days_left <= 14 else 25 if days_left <= 30 else 0)
                trend_score    = min(100, max(0, 50 + (trend_val * 2)))

                importance = round(
                    demand_score * 0.50 + stockout_score * 0.25 + trend_score * 0.10, 2
                )

                results.append({
                    'item_id': item_id,
                    'name': name,
                    'category': cat,
                    'importance': importance,
                    'stockout_risk': risk,
                    'weekly_demand': weekly_demand,
                    'days_left': days_left,
                    'current_loc': c_loc
                })
            except Exception:
                continue

        results.sort(key=lambda x: x['importance'], reverse=True)

        suggestions = []
        for i, p in enumerate(results):
            try:
                if i < len(enriched_boxes):
                    box = enriched_boxes[i]
                    rack_code  = f"R{str(box['rack_code']).zfill(2)}"
                    shelf_code = f"{rack_code}-SH{str(box['shelf_code']).zfill(2)}"
                    box_code   = f"B{str(box['box_code']).zfill(2)}"
                    ideal_loc  = f"{rack_code} {shelf_code} {box_code}"
                else:
                    ideal_loc = "No box available"

                suggestions.append({
                    'product': p['name'],
                    'from': p['current_loc'],
                    'to': ideal_loc,
                    'risk': p['stockout_risk'],
                    'weekly_demand': p['weekly_demand'],
                    'days_of_stock': p['days_left'],
                    'importance': p['importance'],
                    'action': (
                        'MOVE' if p['current_loc'] != ideal_loc and p['current_loc'] != 'Unassigned'
                        else 'ASSIGN' if p['current_loc'] == 'Unassigned'
                        else 'OK'
                    )
                })
            except Exception:
                continue

        print(f"Optimization complete. Returning {len(suggestions)} suggestions.")
        ordered_items = [s['product'] for s in suggestions]

        print("Final arranged order:")
        for i, name in enumerate(ordered_items, 1):
            print(f"{i}. {name}")
        return {"total": len(suggestions), "suggestions": suggestions}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Critical Failure: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")