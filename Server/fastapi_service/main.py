import os
import joblib
import numpy  as np
import pandas as pd
import xgboost as xgb

from dotenv           import load_dotenv
from fastapi          import FastAPI, HTTPException, Body
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

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        shelf_map = {str(s.get('id')): s for s in Shelves}
        rack_map  = {str(r.get('id')): r for r in Racks}

        # ---------- CLEAN FUNCTION ----------
        def clean_code(val):
            if not val:
                return "00"
            val = str(val)
            return ''.join(filter(str.isdigit, val)).zfill(2)

        # ---------- BUILD BOXES ----------
        enriched_boxes = []
        for box in Boxes:
            shelf = shelf_map.get(str(box.get('shelf_id')), {})
            rack  = rack_map.get(str(shelf.get('rack_id')), {})

            enriched_boxes.append({
                'box_id': str(box.get('id')),
                'rack_code': clean_code(rack.get('rack_code')),
                'shelf_code': clean_code(shelf.get('shelf_code')),
                'box_code': clean_code(box.get('box_code')),
                'rack_order': int(rack.get('position') or rack.get('id') or 0),
                'shelf_order': int(shelf.get('position') or shelf.get('id') or 0),
                'box_order': int(box.get('position') or box.get('id') or 0),
            })

        enriched_boxes.sort(key=lambda b: (b['rack_order'], b['shelf_order'], b['box_order']))

        if not enriched_boxes:
            raise HTTPException(status_code=500, detail="No boxes found")

        # ---------- LOCATION FORMAT ----------
        def build_loc(b):
            rack  = f"R{b['rack_code']}"
            shelf = f"SH{b['shelf_code']}"
            box   = f"B{b['box_code']}"
            return f"{rack}-{shelf}-{box}"

        # ---------- CURRENT LOC ----------
        product_current_loc = {}
        for product in Products:
            pid = str(product.get('product_code')).replace('P', '').lstrip('0')

            box_id = str(product.get('box_id', ''))
            if box_id and box_id != 'None':
                box = next((b for b in enriched_boxes if b['box_id'] == box_id), None)
                product_current_loc[pid] = build_loc(box) if box else "Unassigned"
            else:
                product_current_loc[pid] = "Unassigned"

        # ---------- SALES ----------
        sales_lookup = {}
        df = pd.DataFrame(Salesdata)

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id']   = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        df = df[df['lag_7'].notna()]
        latest_df = df.sort_values('sale_date').groupby('item_id').last().reset_index()

        for _, row in latest_df.iterrows():
            sales_lookup[row['item_id']] = {f: float(row[f]) for f in FEATURES}

        # ---------- PROCESS ----------
        results = []

        for product in Products:
            try:
                item_id = str(product.get('product_code')).replace('P', '').lstrip('0')

                if item_id not in sales_lookup:
                    continue

                name  = product.get('product_name', f'Item {item_id}')
                cat   = product.get('category', '')
                stock = float(product.get('stock_qty', 0))
                c_loc = product_current_loc.get(item_id, 'Unassigned')

                feat = sales_lookup[item_id]
                X = np.array([[feat[f] for f in FEATURES]], dtype=np.float32)

                if _scaler:
                    X = _scaler.transform(X)

                weekly_demand = round(float(_model.predict(X)[0]))
                daily_rate    = weekly_demand / 7
                days_left     = round(stock / max(daily_rate, 0.1))

                # ---------- RISK ----------
                if days_left <= 3:
                    risk = 'CRITICAL'
                elif days_left <= 7:
                    risk = 'HIGH'
                elif days_left <= 14:
                    risk = 'MEDIUM'
                else:
                    risk = 'LOW'

                importance = round(
                    min(100, (weekly_demand / 500) * 100) * 0.5 +
                    (100 if days_left <= 3 else 80 if days_left <= 7 else 50 if days_left <= 14 else 25) * 0.25 +
                    min(100, max(0, 50 + feat['trend_direction'] * 2)) * 0.1,
                    2
                )

                results.append({
                    'item_id': item_id,
                    'name': name,
                    'importance': importance,
                    'stockout_risk': risk,
                    'weekly_demand': weekly_demand,
                    'days_left': days_left,
                    'current_loc': c_loc
                })

            except Exception as e:
                print("ERROR:", e)
                continue

        # ---------- SORT ----------
        results.sort(key=lambda x: x['importance'], reverse=True)

        # ---------- SUGGESTIONS ----------
        suggestions = []

        for i, p in enumerate(results):
            if i < len(enriched_boxes):
                ideal_loc = build_loc(enriched_boxes[i])
            else:
                ideal_loc = "No box available"

            action = (
                'MOVE' if p['current_loc'] != ideal_loc and p['current_loc'] != 'Unassigned'
                else 'ASSIGN' if p['current_loc'] == 'Unassigned'
                else 'OK'
            )

            if i < len(enriched_boxes):
                target_box = enriched_boxes[i]
                target_box_id = target_box["box_id"]
            else:
                target_box_id = None

            suggestions.append({
                'product': p['name'],
                'product_code': f"P{p['item_id'].zfill(3)}",  # ✅ REQUIRED for DB update
                'box_id': target_box_id,                      # ✅ REQUIRED for DB update
                'from': p['current_loc'],
                'to': ideal_loc,
                'risk': p['stockout_risk'],
                'weekly_demand': p['weekly_demand'],
                'days_of_stock': p['days_left'],
                'importance': p['importance'],
                'action': action
            })

        return {
            "total": len(suggestions),
            "suggestions": suggestions
        }

    except Exception as e:
        print("Critical Failure:", e)
        raise HTTPException(status_code=500, detail="Internal server error")  
    
    

@app.post("/apply_layout_button")
def apply_layout_button(data: list = Body(...)):
    try:
        if not data:
            return {"status": "no updates"}

        count = 0

        with engine.begin() as conn:
            for u in data:
                product = u.get("product")
                box_id  = u.get("to_box_id")

                if not product or not box_id:
                    print("Skipping invalid row:", u)
                    continue

                conn.execute(
                    text("""
                        UPDATE products 
                        SET box_id = :box_id 
                        WHERE product_name = :product
                    """),
                    {
                        "box_id": box_id,
                        "product": product
                    }
                )

                count += 1

        return {"status": "applied", "count": count}

    except Exception as e:
        print("Apply Error:", e)
        raise HTTPException(status_code=500, detail="Apply failed")
    
@app.post("/optimize_raw_materials")
def optimize_raw_materials():
    try:
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        shelf_map = {str(s.get('id')): s for s in Shelves}
        rack_map  = {str(r.get('id')): r for r in Racks}

        # ---------- LOCATION BUILDER (FIXED FORMAT) ----------
        def build_loc(box):
            if not box:
                return "Unassigned"

            shelf = shelf_map.get(str(box.get('shelf_id')), {})
            rack  = rack_map.get(str(shelf.get('rack_id')), {})

            if not rack or not shelf:
                return "Unassigned"

            rack_code  = f"R{str(rack.get('rack_code')).zfill(2)}"
            shelf_code = f"{rack_code}-SH{str(shelf.get('shelf_code')).zfill(2)}"
            box_code   = f"B{str(box.get('box_code')).zfill(2)}"

            return f"{rack_code} {shelf_code} {box_code}"

        # ---------- SORT BOXES BY POSITION ----------
        enriched_boxes = []
        for box in Boxes:
            shelf = shelf_map.get(str(box.get('shelf_id')), {})
            rack  = rack_map.get(str(shelf.get('rack_id')), {})

            enriched_boxes.append({
                "box": box,
                "order": (
                    int(rack.get('position') or rack.get('id') or 0),
                    int(shelf.get('position') or shelf.get('id') or 0),
                    int(box.get('position') or box.get('id') or 0)
                )
            })

        enriched_boxes.sort(key=lambda x: x["order"])

        # ---------- DEMAND CALCULATION ----------
        df = pd.DataFrame(Salesdata)
        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str).str.strip()
        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        df = df[df['lag_7'].notna()]
        latest_df = df.sort_values('sale_date').groupby('item_id').last().reset_index()

        product_demand = {}
        for _, row in latest_df.iterrows():
            X = np.array([[row[f] for f in FEATURES]], dtype=np.float32)
            if _scaler:
                X = _scaler.transform(X)
            demand = float(_model.predict(X)[0])
            product_demand[row['item_id']] = demand

        # ---------- RAW MATERIAL USAGE (FIXED) ----------
        raw_usage = {}

        for rm in RawMaterials:
            rm_id = str(rm.get('id'))

            linked_products = []

            # ✅ SUPPORT BOTH COLUMN TYPES
            if rm.get("product_ids"):
                linked_products = str(rm.get("product_ids")).split(",")
            elif rm.get("product_id"):
                linked_products = [str(rm.get("product_id"))]

            total_demand = 0
            for pid in linked_products:
                pid = pid.strip()
                if pid in product_demand:
                    total_demand += product_demand[pid]

            raw_usage[rm_id] = total_demand

        # ---------- SORT RAW MATERIALS ----------
        sorted_rm = sorted(
            RawMaterials,
            key=lambda r: raw_usage.get(str(r.get('id')), 0),
            reverse=True
        )

        suggestions = []

        with engine.begin() as conn:
            for i, rm in enumerate(sorted_rm):
                rm_id = str(rm.get('id'))
                name  = rm.get('material_name', f'RM {rm_id}')

                # CURRENT LOCATION
                current_box = next(
                    (b for b in Boxes if str(b.get("id")) == str(rm.get("box_id"))),
                    None
                )
                from_loc = build_loc(current_box)

                # TARGET LOCATION
                if i < len(enriched_boxes):
                    target_box = enriched_boxes[i]["box"]
                    to_loc = build_loc(target_box)

                    # UPDATE DB
                    conn.execute(
                        text("UPDATE raw_materials SET box_id = :box_id WHERE id = :id"),
                        {"box_id": target_box.get("id"), "id": rm_id}
                    )
                else:
                    to_loc = "No box available"

                suggestions.append({
                    "material": name,
                    "from": from_loc,
                    "to": to_loc,
                    "usage_score": raw_usage.get(rm_id, 0)
                })

        return {
            "total": len(suggestions),
            "suggestions": suggestions
        }

    except Exception as e:
        print("RM Optimization Error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")