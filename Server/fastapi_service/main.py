import os
import joblib
import numpy  as np
import pandas as pd
import xgboost as xgb
from datetime import datetime, timedelta
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
                item_id = u.get("id")
                item_type = u.get("type")
                box_id = u.get("box_id")

                if not item_id or not box_id or not item_type:
                    print("Skipping invalid row:", u)
                    continue

                # 📦 PRODUCTS
                if item_type == "product":
                    conn.execute(
                        text("""
                            UPDATE products 
                            SET box_id = :box_id 
                            WHERE product_code = :id
                        """),
                        {"box_id": box_id, "id": item_id}
                    )

                # 🧱 RAW MATERIALS
                elif item_type == "raw_material":
                    conn.execute(
                        text("""
                            UPDATE raw_materials 
                            SET box_id = :box_id 
                            WHERE id = :id
                        """),
                        {"box_id": box_id, "id": item_id}
                    )

                else:
                    print("Unknown type:", item_type)
                    continue

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

        # ---------- RAW MATERIAL USAGE ----------
        raw_usage = {}

        for rm in RawMaterials:
            rm_id = str(rm.get('id'))

            linked_products = []

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

        # ---------- GET CURRENTLY USED BOXES ----------
        used_boxes = [
            b for b in Boxes
            if any(str(rm.get("box_id")) == str(b.get("id")) for rm in RawMaterials)
        ]

        # ---------- SORT USED BOXES BY POSITION ----------
        used_boxes_sorted = sorted(
            used_boxes,
            key=lambda b: (
                int(rack_map.get(str(shelf_map.get(str(b.get('shelf_id')), {}).get('rack_id')), {}).get('position') or 0),
                int(shelf_map.get(str(b.get('shelf_id')), {}).get('position') or 0),
                int(b.get('position') or b.get('id') or 0)
            )
        )

        # ---------- SWAP ASSIGNMENT ----------
        for i in range(min(len(sorted_rm), len(used_boxes_sorted))):

            rm = sorted_rm[i]
            target_box = used_boxes_sorted[i]

            rm_id = str(rm.get('id'))
            name  = rm.get('material_name', f'RM {rm_id}')

            # CURRENT LOCATION
            current_box = next(
                (b for b in Boxes if str(b.get("id")) == str(rm.get("box_id"))),
                None
            )
            from_loc = build_loc(current_box)

            # TARGET LOCATION (SWAP ONLY)
            to_loc = build_loc(target_box)
            target_box_id = target_box.get("id")

            suggestions.append({
                "material": name,
                "material_id": rm_id,
                "from": from_loc,
                "to": to_loc,
                "to_box_id": target_box_id,
                "usage_score": raw_usage.get(rm_id, 0)
            })

        return {
            "total": len(suggestions),
            "suggestions": suggestions
        }

    except Exception as e:
        print("RM Optimization Error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")
    
    
    
    

@app.get("/warehouse_efficiency")
def warehouse_efficiency():
    try:
        # 🔥 LOAD DATA (for total count)
        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        total_products = len(Products)
        total_rm       = len(RawMaterials)

        total_items = total_products + total_rm

        # 🔥 GET SUGGESTIONS
        product_data = optimize_layout()
        rm_data      = optimize_raw_materials()

        product_suggestions = product_data.get("suggestions", [])
        rm_suggestions      = rm_data.get("suggestions", [])

        # 🔥 COUNT ONLY ACTIONABLE SUGGESTIONS
        product_moves = [
            s for s in product_suggestions
            if s.get("action") in ["MOVE", "ASSIGN"]
        ]
        print(product_moves)

        rm_moves = [
            s for s in rm_suggestions
            if s.get("from") != s.get("to")   # RM logic
        ]

        total_suggestions = len(product_moves) + len(rm_moves)
        print(total_suggestions,total_items)
        # 🔥 EFFICIENCY CALCULATION
        if total_items == 0:
            efficiency = 100
        else:
            efficiency = round(
                ((total_items - total_suggestions) / total_items) * 100,
                1
            )

        # 🔥 LABEL (optional but useful)
        if efficiency >= 85:
            label = "Excellent"
        elif efficiency >= 70:
            label = "Good"
        elif efficiency >= 50:
            label = "Average"
        else:
            label = "Poor"

        return {
            "efficiency": efficiency,
            "label": label,
            "total_items": total_items,
            "total_products": total_products,
            "total_raw_materials": total_rm,
            "suggestions": total_suggestions
        }

    except Exception as e:
        print("Efficiency Error:", e)
        raise HTTPException(status_code=500, detail="Failed to calculate efficiency")
    
    
    

@app.post("/predict_until_date")
def predict_until_date(data: dict = Body(...)):
    target_date = data.get("target_date")

    try:

        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        # ---------------- LOAD DATA ----------------
        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        if not Products:
            return {
                "status": "empty",
                "message": "No products found"
            }

        # ---------------- DATE ----------------
        target_date = datetime.strptime(
            target_date,
            "%Y-%m-%d"
        )

        today = datetime.today()

        days_ahead = (target_date - today).days

        if days_ahead <= 0:
            raise HTTPException(
                status_code=400,
                detail="Target date must be in the future"
            )

        num_weeks = max(1, round(days_ahead / 7))

        # ---------------- SALES DATAFRAME ----------------
        df = pd.DataFrame(Salesdata)

        if df.empty:
            raise HTTPException(
                status_code=500,
                detail="No sales data found"
            )

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col],
                    errors='coerce'
                ).fillna(0.0)

        df = df[df['lag_7'].notna()]

        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        # ---------------- FEATURE UPDATE ----------------
        def update_features(f, weekly_pred, week):

            f = f.copy()

            # VERY SMALL drift adjustments only
            drift = min(week * 0.015, 0.12)

            # keep lag features mostly stable
            f['lag_7'] = (
                f['lag_7'] * (1 - drift)
            ) + (
                (weekly_pred / 7) * drift
            )

            f['lag_14'] = (
                f['lag_14'] * (1 - drift)
            ) + (
                f['lag_7'] * drift
            )

            # rolling means barely move
            f['rolling_mean_7'] = (
                f['rolling_mean_7'] * 0.95
            ) + (
                (weekly_pred / 7) * 0.05
            )

            f['rolling_mean_30'] = (
                f['rolling_mean_30'] * 0.98
            ) + (
                (weekly_pred / 7) * 0.02
            )

            # stable trend
            f['trend_direction'] = (
                f['rolling_mean_7']
                - f['rolling_mean_30']
            )

            return f

        # ---------------- BUILD PRODUCTS ----------------
        predictions = []

        for product in Products:

            try:

                code = str(product.get("product_code", ""))
                item_id = code.replace("P", "").lstrip("0")

                row = latest_df[
                    latest_df['item_id'] == item_id
                ]

                if row.empty:
                    continue

                row = row.iloc[0]

                current_features = {
                    feat: float(row[feat])
                    for feat in FEATURES
                }

                weekly_forecast = []

                # -------- MULTI WEEK PREDICTION --------
                for week in range(num_weeks):

                    X = np.array([[
                        current_features[f]
                        for f in FEATURES
                    ]], dtype=np.float32)

                    if _scaler:
                        X = _scaler.transform(X)

                    pred = max(
                        0,
                        round(float(_model.predict(X)[0]))
                    )


                    forecast_date = today + timedelta(weeks=week)

                    week_in_month = ((forecast_date.day - 1) // 7) + 1

                    label = (
                        f"{forecast_date.strftime('%b %Y')} "
                        f"- W{week_in_month}"
                    )

                    weekly_forecast.append({
                        "week": week + 1,
                        "label": label,
                        "forecast_date": forecast_date.strftime("%Y-%m-%d"),
                        "predicted_units": pred
                    })

                    current_features = update_features(
                        current_features,
                        pred,
                        week
                    )

                total_demand = sum(
                    w["predicted_units"]
                    for w in weekly_forecast
                )

                predictions.append({
                    "product_code": code,
                    "product_name": product.get("product_name"),
                    "category": product.get("category"),
                    "current_stock": product.get("stock_qty", 0),
                    "total_predicted_demand": total_demand,
                    "weekly_forecast": weekly_forecast
                })

            except Exception as e:
                print("Prediction Error:", e)
                continue

        # ---------------- SORT ----------------
        predictions.sort(
            key=lambda x: x["total_predicted_demand"],
            reverse=True
        )

        # ---------------- SUMMARY ----------------
        total_inventory_demand = sum(
            p["total_predicted_demand"]
            for p in predictions
        )

        high_demand = [
            p for p in predictions
            if p["total_predicted_demand"] >= 500
        ]

        return {
            "target_date": target_date,
            "weeks": num_weeks,
            "total_products": len(predictions),
            "total_inventory_demand": total_inventory_demand,
            "high_demand_products": len(high_demand),
            "predictions": predictions
        }

    except Exception as e:
        print("Forecast Error:", e)
        raise HTTPException(
            status_code=500,
            detail="Forecast generation failed"
        )
        
        
        
        

def update_features_for_next_week(f, weekly_sales):

    new_f = f.copy()

    daily_avg = weekly_sales / 7

    # lag updates
    new_f['lag_30'] = new_f.get('lag_14', 0)
    new_f['lag_14'] = new_f.get('lag_7', 0)
    new_f['lag_7'] = daily_avg

    # rolling averages
    new_f['rolling_mean_7'] = daily_avg

    new_f['rolling_mean_30'] = (
        (new_f.get('rolling_mean_30', 0) * 23)
        + (daily_avg * 7)
    ) / 30

    new_f['rolling_mean_90'] = (
        (new_f.get('rolling_mean_90', 0) * 83)
        + (daily_avg * 7)
    ) / 90

    # trend
    raw_trend = (
        new_f['lag_7']
        - new_f['lag_30']
    )

    new_f['trend_direction'] = max(
        min(raw_trend, 5.0),
        -5.0
    )

    # date features
    new_f['day_of_week'] = (
        new_f.get('day_of_week', 0) + 7
    ) % 7

    new_f['is_weekend'] = (
        1 if new_f['day_of_week'] >= 5 else 0
    )

    # month + quarter
    new_f['month'] = (
        new_f.get('month', 1) + 0.25
    )

    if new_f['month'] > 12.75:
        new_f['month'] = 1

    new_f['quarter'] = (
        (int(new_f['month']) - 1) // 3
    ) + 1

    return new_f

@app.post("/simulate_demand_spike")
def simulate_demand_spike(data: dict = Body(...)):

    try:

        if _model is None:
            raise HTTPException(
                status_code=503,
                detail="Model not loaded"
            )

        product_code = data.get("product_code")
        spike_percent = float(data.get("spike_percent", 50))
        spike_duration_weeks = int(
            data.get("spike_duration_weeks", 2)
        )

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        df = pd.DataFrame(Salesdata)

        if df.empty:
            raise HTTPException(
                status_code=500,
                detail="No sales data found"
            )

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col],
                    errors='coerce'
                ).fillna(0.0)

        latest_df = (
            df.sort_values("sale_date")
              .groupby("item_id")
              .last()
              .reset_index()
        )

        item_id = str(product_code).replace("P", "").lstrip("0")

        row = latest_df[
            latest_df["item_id"] == item_id
        ]

        if row.empty:
            raise HTTPException(
                status_code=404,
                detail="Product sales history not found"
            )

        row = row.iloc[0]

        feature_dict = {
            feat: float(row[feat])
            for feat in FEATURES
        }

        product = next(
            (
                p for p in Products
                if p.get("product_code") == product_code
            ),
            None
        )

        product_name = (
            product.get("product_name")
            if product else product_code
        )

        # =====================================
        # FEATURE UPDATE
        # =====================================

        def update_features_for_next_week(
            f,
            weekly_sales
        ):

            new_f = f.copy()

            daily_avg = weekly_sales / 7

            new_f['lag_30'] = new_f['lag_14']
            new_f['lag_14'] = new_f['lag_7']
            new_f['lag_7'] = daily_avg

            new_f['rolling_mean_7'] = daily_avg

            new_f['rolling_mean_30'] = (
                (
                    new_f['rolling_mean_30'] * 23
                ) + (
                    daily_avg * 7
                )
            ) / 30

            new_f['rolling_mean_90'] = (
                (
                    new_f['rolling_mean_90'] * 83
                ) + (
                    daily_avg * 7
                )
            ) / 90

            raw_trend = (
                new_f['lag_7']
                - new_f['lag_30']
            )

            new_f['trend_direction'] = max(
                min(raw_trend, 5.0),
                -5.0
            )

            new_f['day_of_week'] = (
                new_f['day_of_week'] + 7
            ) % 7

            new_f['is_weekend'] = (
                1 if new_f['day_of_week'] >= 5 else 0
            )

            new_f['month'] += 0.25

            if new_f['month'] > 12.75:
                new_f['month'] = 1

            new_f['quarter'] = (
                (int(new_f['month']) - 1) // 3
            ) + 1

            return new_f

        # =====================================
        # PREDICTION
        # =====================================

        def get_prediction(features):

            X = np.array([[
                features[f]
                for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X = _scaler.transform(X)

            pred = float(_model.predict(X)[0])

            return max(0, round(pred))

        # =====================================
        # SIMULATION
        # =====================================

        num_weeks = 6

        baseline = []
        spike = []

        f_normal = feature_dict.copy()
        f_spike = feature_dict.copy()

        multiplier = 1 + (
            spike_percent / 100
        )

        for w in range(num_weeks):

            # ---------------- BASELINE ----------------

            normal_pred = get_prediction(
                f_normal
            )

            baseline.append({
                "week": w + 1,
                "predicted_units": normal_pred
            })

            f_normal = update_features_for_next_week(
                f_normal,
                normal_pred
            )

            # ---------------- SPIKE ----------------

            spike_base = get_prediction(
                f_spike
            )

            if w < spike_duration_weeks:

                spike_final = round(
                    spike_base * multiplier
                )

            else:

                spike_final = spike_base

            spike.append({
                "week": w + 1,
                "predicted_units": spike_final
            })

            f_spike = update_features_for_next_week(
                f_spike,
                spike_final
            )

        # =====================================
        # SUMMARY
        # =====================================

        baseline_total = sum(
            x["predicted_units"]
            for x in baseline
        )

        spike_total = sum(
            x["predicted_units"]
            for x in spike
        )

        return {

            "product_code": product_code,

            "product_name": product_name,

            "spike_percent": spike_percent,

            "spike_duration_weeks":
                spike_duration_weeks,

            "baseline_total":
                baseline_total,

            "spike_total":
                spike_total,

            "extra_demand":
                spike_total - baseline_total,

            "baseline_forecast":
                baseline,

            "spike_forecast":
                spike
        }

    except Exception as e:

        print("Demand Spike Error:", e)

        raise HTTPException(
            status_code=500,
            detail="Demand spike simulation failed"
        )
        
        
@app.get("/get-products")
def get_products():

    try:

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        if not Products:
            return []

        cleaned_products = []

        for p in Products:

            cleaned_products.append({

                "product_code":
                    p.get("product_code", ""),

                "product_name":
                    p.get("product_name", ""),

                "category":
                    p.get("category", ""),

                "stock_qty":
                    p.get("stock_qty", 0),

                "price":
                    p.get("price", 0)
            })

        return cleaned_products

    except Exception as e:

        print("Get Products Error:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to load products"
        )
        
        
@app.post("/simulate_supply_delay")
def simulate_supply_delay(data: dict = Body(...)):

    product_code = data.get("product_code")
    current_stock = int(data.get("current_stock", 0))
    restock_qty = int(data.get("restock_qty", 0))
    delay_weeks = int(data.get("delay_weeks", 0))

    try:

        if _model is None:
            raise HTTPException(
                status_code=503,
                detail="Model not loaded"
            )

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        df = pd.DataFrame(Salesdata)

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col],
                    errors='coerce'
                ).fillna(0.0)

        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        item_id = product_code.replace("P", "").lstrip("0")

        row = latest_df[
            latest_df['item_id'] == item_id
        ]

        if row.empty:
            raise HTTPException(
                status_code=404,
                detail="Product sales history not found"
            )

        row = row.iloc[0]

        current_features = {
            feat: float(row[feat])
            for feat in FEATURES
        }

        product = next(
            (
                p for p in Products
                if p.get("product_code") == product_code
            ),
            {}
        )

        product_name = product.get(
            "product_name",
            product_code
        )

        num_weeks = 8
        arrival_week = 2 + delay_weeks

        inventory = current_stock

        weekly_forecast = []

        total_shortage = 0
        stockout_weeks = 0

        for week in range(1, num_weeks + 1):

            X = np.array([[
                current_features[f]
                for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X = _scaler.transform(X)

            predicted_demand = max(
                0,
                round(float(_model.predict(X)[0]))
            )

            restock_arrived = False

            if week == arrival_week:
                inventory += restock_qty
                restock_arrived = True

            inventory_before = inventory

            actual_sales = min(
                inventory,
                predicted_demand
            )

            shortage = max(
                0,
                predicted_demand - inventory
            )

            inventory = max(
                0,
                inventory - predicted_demand
            )

            if inventory == 0:
                stockout_weeks += 1

            total_shortage += shortage

            weekly_forecast.append({
                "week": week,
                "predicted_demand": predicted_demand,
                "actual_sales": actual_sales,
                "shortage": shortage,
                "inventory_before": inventory_before,
                "inventory_after": inventory,
                "restock_arrived": restock_arrived
            })

            current_features = update_features_for_next_week(
                current_features,
                predicted_demand
            )

        return {

            "product_code": product_code,

            "product_name": product_name,

            "current_stock": current_stock,

            "restock_qty": restock_qty,

            "delay_weeks": delay_weeks,

            "arrival_week": arrival_week,

            "total_shortage": total_shortage,

            "stockout_weeks": stockout_weeks,

            "weekly_forecast": weekly_forecast
        }

    except Exception as e:

        print("Supply Delay Error:", e)

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
@app.post("/simulate_seasonal_surge")
def simulate_seasonal_surge(data: dict = Body(...)):

    product_code = data.get("product_code")

    current_stock = int(
        data.get("current_stock", 1500)
    )

    season_name = data.get(
        "season_name",
        "Festival"
    )

    peak_week = int(
        data.get("peak_week", 3)
    )

    peak_boost_percent = float(
        data.get("peak_boost_percent", 120)
    )

    season_duration_weeks = int(
        data.get("season_duration_weeks", 6)
    )

    try:

        if _model is None:
            raise HTTPException(
                status_code=503,
                detail="Model not loaded"
            )

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        df = pd.DataFrame(Salesdata)

        if df.empty:
            raise HTTPException(
                status_code=500,
                detail="No sales data found"
            )

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:

            if col in df.columns:

                df[col] = pd.to_numeric(
                    df[col],
                    errors='coerce'
                ).fillna(0.0)

        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        item_id = (
            product_code
            .replace("P", "")
            .lstrip("0")
        )

        row = latest_df[
            latest_df['item_id'] == item_id
        ]

        if row.empty:

            raise HTTPException(
                status_code=404,
                detail="Product sales history not found"
            )

        row = row.iloc[0]

        current_features_normal = {
            feat: float(row[feat])
            for feat in FEATURES
        }

        current_features_surge = {
            feat: float(row[feat])
            for feat in FEATURES
        }

        product = next(
            (
                p for p in Products
                if p.get("product_code") == product_code
            ),
            {}
        )

        product_name = product.get(
            "product_name",
            product_code
        )

        # =====================================
        # MULTIPLIERS
        # =====================================

        multipliers = []

        for w in range(season_duration_weeks):

            week_num = w + 1

            if week_num <= peak_week:

                progress = (
                    week_num / peak_week
                )

                boost = (
                    peak_boost_percent / 100
                ) * progress

            else:

                remaining = max(
                    1,
                    season_duration_weeks - peak_week
                )

                progress = (
                    (week_num - peak_week)
                    / remaining
                )

                boost = (
                    peak_boost_percent / 100
                ) * (1 - progress)

            multipliers.append(
                round(1 + boost, 2)
            )

        # =====================================
        # FORECASTS
        # =====================================

        weekly_forecast = []

        stock_normal = current_stock
        stock_surge = current_stock

        stockout_week = None

        for w in range(season_duration_weeks):

            # ---------------- NORMAL ----------------

            X_normal = np.array([[
                current_features_normal[f]
                for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X_normal = _scaler.transform(
                    X_normal
                )

            normal_pred = max(
                0,
                round(
                    float(
                        _model.predict(
                            X_normal
                        )[0]
                    )
                )
            )

            # ---------------- SURGE ----------------

            X_surge = np.array([[
                current_features_surge[f]
                for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X_surge = _scaler.transform(
                    X_surge
                )

            surge_base = max(
                0,
                round(
                    float(
                        _model.predict(
                            X_surge
                        )[0]
                    )
                )
            )

            surge_pred = round(
                surge_base * multipliers[w]
            )

            # ---------------- STOCK ----------------

            stock_before_normal = stock_normal
            stock_before_surge = stock_surge

            stock_normal = max(
                0,
                stock_normal - normal_pred
            )

            stock_surge = max(
                0,
                stock_surge - surge_pred
            )

            if (
                stock_surge <= 0
                and stockout_week is None
            ):
                stockout_week = w + 1

            # ---------------- SAVE ----------------

            weekly_forecast.append({

                "week": w + 1,

                "multiplier": multipliers[w],

                "normal_prediction": normal_pred,

                "surge_prediction": surge_pred,

                "normal_stock_before": stock_before_normal,

                "normal_stock_after": stock_normal,

                "surge_stock_before": stock_before_surge,

                "surge_stock_after": stock_surge
            })

            # ---------------- UPDATE FEATURES ----------------

            current_features_normal = (
                update_features_for_next_week(
                    current_features_normal,
                    normal_pred
                )
            )

            current_features_surge = (
                update_features_for_next_week(
                    current_features_surge,
                    surge_pred
                )
            )

        # =====================================
        # RESPONSE
        # =====================================

        return {

            "product_code": product_code,

            "product_name": product_name,

            "season_name": season_name,

            "current_stock": current_stock,

            "peak_week": peak_week,

            "peak_boost_percent": peak_boost_percent,

            "season_duration_weeks": season_duration_weeks,

            "stockout_week": stockout_week,

            "weekly_forecast": weekly_forecast
        }

    except Exception as e:

        print("Seasonal Surge Error:", e)

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
@app.post("/simulate_new_product_launch")
def simulate_new_product_launch(data: dict = Body(...)):

    product_name = data.get("product_name")

    category = data.get(
        "category",
        "Electronics"
    )

    launch_week_month = int(
        data.get("launch_week_month", 3)
    )

    initial_stock = int(
        data.get("initial_stock", 500)
    )

    growth_rate_percent = float(
        data.get("growth_rate_percent", 15)
    )

    num_weeks = int(
        data.get("num_weeks", 8)
    )

    try:

        category_baselines = {

            'Electronics': {
                'daily_avg': 35,
                'volatility': 0.25
            },

            'Pumps': {
                'daily_avg': 80,
                'volatility': 0.15
            },

            'Mechanical': {
                'daily_avg': 20,
                'volatility': 0.10
            },

            'Machinery': {
                'daily_avg': 12,
                'volatility': 0.30
            }
        }

        baseline = category_baselines.get(
            category,
            category_baselines['Electronics']
        )

        month_index = {

            1: 0.70,
            2: 0.65,
            3: 0.75,
            4: 0.80,
            5: 0.85,
            6: 0.90,
            7: 0.92,
            8: 1.00,
            9: 1.05,
            10: 1.20,
            11: 1.40,
            12: 1.60
        }

        ramp_preds = []

        confidence = []

        current_month = launch_week_month

        base_daily = (
            baseline['daily_avg'] * 0.30
        )

        # =====================================
        # DEMAND RAMP
        # =====================================

        for w in range(num_weeks):

            if w > 0:

                base_daily = (
                    base_daily *
                    (
                        1 + (
                            growth_rate_percent / 100
                        )
                    )
                )

            base_daily = min(
                base_daily,
                baseline['daily_avg'] * 1.10
            )

            seasonal_mod = month_index.get(
                current_month,
                1.0
            )

            weekly_pred = round(
                base_daily * 7 * seasonal_mod
            )

            ramp_preds.append(
                weekly_pred
            )

            conf = min(
                85,
                35 + (w * 7)
            )

            confidence.append(conf)

            if (w + 1) % 4 == 0:

                current_month = (
                    current_month + 1
                    if current_month < 12
                    else 1
                )

        # =====================================
        # MODEL BLEND
        # =====================================

        week4_daily = (
            ramp_preds[3] / 7
            if len(ramp_preds) >= 4
            else base_daily
        )

        model_features = {

            'day_of_week': 0,
            'month': launch_week_month,

            'quarter':
                (
                    (launch_week_month - 1) // 3
                ) + 1,

            'is_weekend': 0,
            'is_month_start': 0,
            'is_month_end': 0,

            'lag_7': week4_daily,
            'lag_14': week4_daily * 0.85,
            'lag_30': week4_daily * 0.65,
            'lag_365': 0,

            'rolling_mean_7':
                week4_daily * 0.95,

            'rolling_mean_30':
                week4_daily * 0.70,

            'rolling_mean_90':
                week4_daily * 0.60,

            'rolling_std_7':
                (
                    week4_daily *
                    baseline['volatility']
                ),

            'trend_direction':
                week4_daily * 0.35,

            'yoy_growth': 0
        }

        for w in range(4, num_weeks):

            X = np.array([[
                model_features[f]
                for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X = _scaler.transform(X)

            model_weekly = max(
                0,
                round(
                    float(
                        _model.predict(X)[0]
                    )
                )
            )

            blend_weight = min(
                1.0,
                (w - 3) * 0.25
            )

            blended = round(
                (
                    ramp_preds[w]
                    * (1 - blend_weight)
                ) +
                (
                    model_weekly
                    * blend_weight
                )
            )

            ramp_preds[w] = blended

        # =====================================
        # STOCK SIMULATION
        # =====================================

        stock_levels = [initial_stock]

        restock_events = []

        weekly_forecast = []

        for w in range(num_weeks):

            stock_before = stock_levels[-1]

            remaining = (
                stock_before -
                ramp_preds[w]
            )

            restocked = False

            restock_qty = 0

            if remaining < (
                initial_stock * 0.20
            ):

                restock_qty = initial_stock

                remaining += restock_qty

                restocked = True

                restock_events.append({

                    'week': w + 1,

                    'qty': restock_qty
                })

            stock_after = max(
                0,
                remaining
            )

            stock_levels.append(
                stock_after
            )

            weekly_forecast.append({

                "week": w + 1,

                "predicted_demand":
                    ramp_preds[w],

                "confidence":
                    confidence[w],

                "stock_before":
                    stock_before,

                "stock_after":
                    stock_after,

                "restocked":
                    restocked,

                "restock_qty":
                    restock_qty
            })



        # =====================================
        # RESPONSE
        # =====================================

        return {

            "product_name":
                product_name,

            "category":
                category,

            "launch_week_month":
                launch_week_month,

            "growth_rate_percent":
                growth_rate_percent,

            "initial_stock":
                initial_stock,

            "weekly_forecast":
                weekly_forecast
        }

    except Exception as e:

        print(
            "New Launch Error:",
            e
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )