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

MODEL_PATH ="./models/xgbmodel.pkl"

FEATURES=[
    'day_of_week','month','quarter','is_weekend',
    'is_month_start','is_month_end','lag_7','lag_14',
    'lag_30','lag_365','rolling_mean_7','rolling_mean_30',
    'rolling_mean_90','rolling_std_7','trend_direction','yoy_growth'
]

DB_URL=URL.create(
    drivername="mysql+pymysql",
    username=os.getenv("DATABASE_USER"),
    password=os.getenv("DATABASE_PASSWORD"),
    host=os.getenv("DATABASE_HOST", "localhost"),
    port=int(os.getenv("DATABASE_PORT", 3306)),
    database=os.getenv("DATABASE_NAME")
)
engine=create_engine(DB_URL, pool_pre_ping=True)
_model =None
_scaler=None
def load_model():
    global _model
    if not os.path.exists(MODEL_PATH):
        #print(f"Model file not found: {MODEL_PATH}")
        return
    _model=joblib.load(MODEL_PATH)
    #print(f"XGBoost model loaded from {MODEL_PATH}")
load_model()

app=FastAPI(title="Warehouse ML API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# m=joblib.load("./models/xgbmodel.pkl")
# #print(type(m)) 
def load_data():
    Products, Boxes, Shelves, Racks, RawMaterials, Salesdata=[], [], [], [], [], []
    with engine.connect() as conn:
        Products    =[dict(r) for r in conn.execute(text("SELECT*FROM products")).mappings().all()]
        Boxes       =[dict(r) for r in conn.execute(text("SELECT*FROM boxes")).mappings().all()]
        Shelves     =[dict(r) for r in conn.execute(text("SELECT*FROM shelves")).mappings().all()]
        Racks       =[dict(r) for r in conn.execute(text("SELECT*FROM racks")).mappings().all()]
        RawMaterials=[dict(r) for r in conn.execute(text("SELECT*FROM raw_materials")).mappings().all()]
        Salesdata   =[dict(r) for r in conn.execute(text("SELECT*FROM sales_data")).mappings().all()]
    return Products, Boxes, Shelves, Racks, RawMaterials, Salesdata

@app.get("/get-warehouse-data")
def get_warehouse_data():
    Products, Boxes, Shelves, Racks, RawMaterials=[], [], [], [], []
    with engine.connect() as conn:
        Products    =[dict(r) for r in conn.execute(text("SELECT*FROM products")).mappings().all()]
        Boxes       =[dict(r) for r in conn.execute(text("SELECT*FROM boxes")).mappings().all()]
        Shelves     =[dict(r) for r in conn.execute(text("SELECT*FROM shelves")).mappings().all()]
        Racks       =[dict(r) for r in conn.execute(text("SELECT*FROM racks")).mappings().all()]
        RawMaterials=[dict(r) for r in conn.execute(text("SELECT*FROM raw_materials")).mappings().all()]
    return {
        "racks": Racks,
        "shelves": Shelves,
        "boxes": Boxes,
        "products": Products,
        "raw_materials": RawMaterials,
    }
    
@app.post("/optimizelayout")
def optimize_layout():

    
    try:
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        shelf_map = {str(s.get('id')): s for s in Shelves}
        rack_map = {str(r.get('id')): r for r in Racks}

        box_sorter = _BoxLocationBuilder(shelf_map, rack_map)
        enriched_boxes = box_sorter.build_sorted_boxes(Boxes)

        if not enriched_boxes:
            raise HTTPException(status_code=500, detail="No boxes found")

        # current product locations
        product_current_loc = _map_product_locations(
            Products,
            enriched_boxes,
            box_sorter
        )

        # demand features from sales history
        sales_features = _extract_sales_features(Salesdata)

        # rank products by stockout risk + demand
        results = []

        for product in Products:
            try:
                item_id = str(product.get('product_code')).replace('P', '').lstrip('0')

                if item_id not in sales_features:
                    continue

                name = product.get('product_name', f'Item {item_id}')
                stock = float(product.get('stock_qty', 0))
                current_loc = product_current_loc.get(item_id, 'Unassigned')

                features = sales_features[item_id]
                weekly_demand = _predict_weekly_demand(features, _model, _scaler)
                daily_rate = weekly_demand / 7
                days_left = round(stock / max(daily_rate, 0.1))

                stockout_risk = _calculate_stockout_risk(days_left)

                importance = _calculate_importance_score(
                    weekly_demand,
                    days_left,
                    features.get('trend_direction', 0)
                )

                results.append({
                    'item_id': item_id,
                    'name': name,
                    'importance': importance,
                    'stockout_risk': stockout_risk,
                    'weekly_demand': weekly_demand,
                    'days_left': days_left,
                    'current_loc': current_loc
                })

            except Exception as e:
                print(f"Skipping {product.get('product_code')}: {e}")
                continue


        results.sort(key=lambda x: x['importance'], reverse=True)


        suggestions = []

        for i, product_result in enumerate(results):
            ideal_box = enriched_boxes[i] if i < len(enriched_boxes) else None
            ideal_loc = box_sorter.build_location(ideal_box) if ideal_box else "No box available"
            target_box_id = ideal_box["box_id"] if ideal_box else None

            action = _determine_action(
                product_result['current_loc'],
                ideal_loc
            )

            suggestions.append({
                'product': product_result['name'],
                'product_code': f"P{product_result['item_id'].zfill(3)}",
                'box_id': target_box_id,
                'from': product_result['current_loc'],
                'to': ideal_loc,
                'risk': product_result['stockout_risk'],
                'weekly_demand': product_result['weekly_demand'],
                'days_of_stock': product_result['days_left'],
                'importance': product_result['importance'],
                'action': action
            })

        return {
            "total": len(suggestions),
            "suggestions": suggestions
        }

    except Exception as e:
        print(f"Layout optimization failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


class _BoxLocationBuilder:


    def __init__(self, shelf_map, rack_map):
        self.shelf_map = shelf_map
        self.rack_map = rack_map

    def _clean_code(self, val):

        if not val:
            return "00"
        return ''.join(filter(str.isdigit, str(val))).zfill(2)

    def build_sorted_boxes(self, boxes):

        enriched = []
        
        for box in boxes:
            shelf = self.shelf_map.get(str(box.get('shelf_id')), {})
            rack = self.rack_map.get(str(shelf.get('rack_id')), {})

            enriched.append({
                'box_id': str(box.get('id')),
                'rack_code': self._clean_code(rack.get('rack_code')),
                'shelf_code': self._clean_code(shelf.get('shelf_code')),
                'box_code': self._clean_code(box.get('box_code')),
                'rack_order': int(rack.get('position') or rack.get('id') or 0),
                'shelf_order': int(shelf.get('position') or shelf.get('id') or 0),
                'box_order': int(box.get('position') or box.get('id') or 0),
            })

        enriched.sort(
            key=lambda b: (b['rack_order'], b['shelf_order'], b['box_order'])
        )
        return enriched

    def build_location(self, box):

        if not box:
            return "Unassigned"
        
        rack = f"R{box['rack_code']}"
        shelf = f"SH{box['shelf_code']}"
        box_code = f"B{box['box_code']}"
        return f"{rack}-{shelf}-{box_code}"


def _map_product_locations(products, enriched_boxes, box_sorter):

    locations = {}
    
    for product in products:
        item_id = str(product.get('product_code')).replace('P', '').lstrip('0')
        box_id = str(product.get('box_id', ''))
        
        if box_id and box_id != 'None':
            box = next(
                (b for b in enriched_boxes if b['box_id'] == box_id),
                None
            )
            locations[item_id] = box_sorter.build_location(box) if box else "Unassigned"
        else:
            locations[item_id] = "Unassigned"
    
    return locations


def _extract_sales_features(salesdata):

    df = pd.DataFrame(salesdata)
    df['sale_date'] = pd.to_datetime(df['sale_date'])
    df['item_id'] = df['item_id'].astype(str)

    for col in FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    df = df[df['lag_7'].notna()]
    latest_df = (
        df.sort_values('sale_date')
          .groupby('item_id')
          .last()
          .reset_index()
    )

    features_dict = {}
    for _, row in latest_df.iterrows():
        features_dict[row['item_id']] = {f: float(row[f]) for f in FEATURES}

    return features_dict


def _predict_weekly_demand(features, model, scaler):

    X = np.array([[features[f] for f in FEATURES]], dtype=np.float32)
    
    if scaler:
        X = scaler.transform(X)
    
    return round(float(model.predict(X)[0]))


def _calculate_stockout_risk(days_left):

    if days_left <= 3:
        return 'CRITICAL'
    elif days_left <= 7:
        return 'HIGH'
    elif days_left <= 14:
        return 'MEDIUM'
    else:
        return 'LOW'


def _calculate_importance_score(weekly_demand, days_left, trend_direction):

    
    
    
    
    
    demand_component = min(100, (weekly_demand / 500) * 100) * 0.5
    
    urgency_map = {
        'CRITICAL': 100,
        'HIGH': 80,
        'MEDIUM': 50,
        'LOW': 25
    }
    risk = _calculate_stockout_risk(days_left)
    urgency_component = urgency_map[risk] * 0.25
    
    trend_component = min(100, max(0, 50 + trend_direction * 2)) * 0.1
    
    return round(demand_component + urgency_component + trend_component, 2)


def _determine_action(current_location, ideal_location):

    if current_location == "Unassigned":
        return "ASSIGN"
    elif current_location != ideal_location:
        return "MOVE"
    else:
        return "OK"
    
@app.post("/apply_layout_button")
def apply_layout_button(data: list = Body(...)):

    
    try:
        if not data:
            return {"status": "no updates"}

        count = 0

        with engine.begin() as conn:
            for update in data:
                item_id = update.get("id")
                item_type = update.get("type")
                box_id = update.get("box_id")

                if not item_id or not box_id or not item_type:
                    continue

                if item_type == "product":
                    conn.execute(
                        text("""
                            UPDATE products 
                            SET box_id = :box_id 
                            WHERE product_code = :id
                        """),
                        {"box_id": box_id, "id": item_id}
                    )
                    count += 1

                elif item_type == "raw_material":
                    conn.execute(
                        text("""
                            UPDATE raw_materials 
                            SET box_id = :box_id 
                            WHERE id = :id
                        """),
                        {"box_id": box_id, "id": item_id}
                    )
                    count += 1

        return {"status": "applied", "count": count}

    except Exception as e:
        print(f"Failed to apply layout changes: {e}")
        raise HTTPException(status_code=500, detail="Apply failed")
    
@app.post("/optimize_raw_materials")
def optimize_raw_materials():

    
    try:
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        shelf_map = {str(s.get('id')): s for s in Shelves}
        rack_map = {str(r.get('id')): r for r in Racks}

        box_sorter = _BoxPositionSorter(shelf_map, rack_map)

        df = pd.DataFrame(Salesdata)
        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str).str.strip()

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        df = df[df['lag_7'].notna()]
        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        # calculate current demand for each product
        product_demand = {}
        for _, row in latest_df.iterrows():
            X = np.array([[row[f] for f in FEATURES]], dtype=np.float32)
            if _scaler:
                X = _scaler.transform(X)
            demand = float(_model.predict(X)[0])
            product_demand[row['item_id']] = demand

        # aggregate demand by raw material
        raw_usage = _calculate_material_usage(
            RawMaterials,
            product_demand
        )

        # rank materials by demand
        sorted_rm = sorted(
            RawMaterials,
            key=lambda r: raw_usage.get(str(r.get('id')), 0),
            reverse=True
        )

        # get currently stored materials
        used_boxes = [
            b for b in Boxes
            if any(
                str(rm.get("box_id")) == str(b.get("id"))
                for rm in RawMaterials
            )
        ]

        used_boxes_sorted = box_sorter.sort_boxes(used_boxes)

        # suggest swaps: high-demand materials → accessible positions
        suggestions = []

        for i in range(min(len(sorted_rm), len(used_boxes_sorted))):
            rm = sorted_rm[i]
            target_box = used_boxes_sorted[i]

            rm_id = str(rm.get('id'))
            current_box = next(
                (b for b in Boxes if str(b.get("id")) == str(rm.get("box_id"))),
                None
            )

            suggestions.append({
                "material": rm.get('material_name', f'RM {rm_id}'),
                "material_id": rm_id,
                "from": box_sorter.build_location(current_box),
                "to": box_sorter.build_location(target_box),
                "to_box_id": target_box.get("id"),
                "usage_score": raw_usage.get(rm_id, 0)
            })

        return {
            "total": len(suggestions),
            "suggestions": suggestions
        }

    except Exception as e:
        print(f"Raw material optimization failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


class _BoxPositionSorter:


    def __init__(self, shelf_map, rack_map):
        self.shelf_map = shelf_map
        self.rack_map = rack_map

    def build_location(self, box):

        if not box:
            return "Unassigned"

        shelf = self.shelf_map.get(str(box.get('shelf_id')), {})
        rack = self.rack_map.get(str(shelf.get('rack_id')), {})

        if not rack or not shelf:
            return "Unassigned"

        rack_code = f"R{str(rack.get('rack_code')).zfill(2)}"
        shelf_code = f"{rack_code}-SH{str(shelf.get('shelf_code')).zfill(2)}"
        box_code = f"B{str(box.get('box_code')).zfill(2)}"

        return f"{rack_code} {shelf_code} {box_code}"

    def _position_tuple(self, box):

        shelf = self.shelf_map.get(str(box.get('shelf_id')), {})
        rack = self.rack_map.get(str(shelf.get('rack_id')), {})

        return (
            int(rack.get('position') or rack.get('id') or 0),
            int(shelf.get('position') or shelf.get('id') or 0),
            int(box.get('position') or box.get('id') or 0)
        )

    def sort_boxes(self, boxes):

        return sorted(boxes, key=self._position_tuple)


def _calculate_material_usage(raw_materials, product_demand):

    raw_usage = {}

    for rm in raw_materials:
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

    return raw_usage
    
    

@app.get("/warehouse_efficiency")
def warehouse_efficiency():
    try:
        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata=load_data()

        total_products=len(Products)
        total_rm=len(RawMaterials)

        total_items=total_products+total_rm
        product_data=optimize_layout()
        rm_data=optimize_raw_materials()

        product_suggestions=product_data.get("suggestions", [])
        rm_suggestions     =rm_data.get("suggestions", [])
        product_moves=[
            s for s in product_suggestions
            if s.get("action") in ["MOVE", "ASSIGN"]
        ]
        #print(product_moves)

        rm_moves=[
            s for s in rm_suggestions
            if s.get("from") != s.get("to")   # RM logic
        ]

        total_suggestions=len(product_moves)+len(rm_moves)
        #print(total_suggestions,total_items)
        if total_items == 0:
            efficiency=100
        else:
            efficiency=round(
                ((total_items-total_suggestions)/total_items)*100,
                1
            )
        if efficiency >= 85:
            label="Excellent"
        elif efficiency >= 70:
            label="Good"
        elif efficiency >= 50:
            label="Average"
        else:
            label="Poor"

        return {
            "efficiency": efficiency,
            "label": label,
            "total_items": total_items,
            "total_products": total_products,
            "total_raw_materials": total_rm,
            "suggestions": total_suggestions
        }

    except Exception as e:
        #print("Efficiency Error:", e)
        raise HTTPException(status_code=500, detail="Failed to calculate efficiency")
    
    
    

@app.post("/predict_until_date")
def predict_until_date(data: dict = Body(...)):

    
    target_date = data.get("target_date")

    try:
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()

        if not Products:
            return {
                "status": "empty",
                "message": "No products found"
            }

        target_date = datetime.strptime(target_date, "%Y-%m-%d")
        today = datetime.today()
        days_ahead = (target_date - today).days

        if days_ahead <= 0:
            raise HTTPException(
                status_code=400,
                detail="Target date must be in the future"
            )

        num_weeks = max(1, round(days_ahead / 7))

        df = pd.DataFrame(Salesdata)

        if df.empty:
            raise HTTPException(status_code=500, detail="No sales data found")

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        df = df[df['lag_7'].notna()]

        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        predictions = []

        for product in Products:
            try:
                code = str(product.get("product_code", ""))
                item_id = code.replace("P", "").lstrip("0")

                row = latest_df[latest_df['item_id'] == item_id]

                if row.empty:
                    continue

                row = row.iloc[0]
                current_features = {
                    feat: float(row[feat]) for feat in FEATURES
                }

                weekly_forecast = _forecast_product_weeks(
                    current_features,
                    num_weeks,
                    today,
                    _model,
                    _scaler
                )

                total_demand = sum(
                    w["predicted_units"] for w in weekly_forecast
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
                print(f"Skipping {product.get('product_code')}: {e}")
                continue

        predictions.sort(
            key=lambda x: x["total_predicted_demand"],
            reverse=True
        )

        total_inventory_demand = sum(
            p["total_predicted_demand"] for p in predictions
        )

        high_demand = [
            p for p in predictions
            if p["total_predicted_demand"] >= 500
        ]

        return {
            "target_date": target_date.strftime("%Y-%m-%d"),
            "weeks": num_weeks,
            "total_products": len(predictions),
            "total_inventory_demand": total_inventory_demand,
            "high_demand_products": len(high_demand),
            "predictions": predictions
        }

    except Exception as e:
        print(f"Forecast generation failed: {e}")
        raise HTTPException(status_code=500, detail="Forecast generation failed")


def _forecast_product_weeks(features, num_weeks, start_date, model, scaler):

    weekly_forecast = []

    for week_idx in range(num_weeks):
        X = np.array([[
            features[f] for f in FEATURES
        ]], dtype=np.float32)

        if scaler:
            X = scaler.transform(X)

        pred = max(0, round(float(model.predict(X)[0])))

        forecast_date = start_date + timedelta(weeks=week_idx)
        week_in_month = ((forecast_date.day - 1) // 7) + 1

        label = f"{forecast_date.strftime('%b %Y')} - W{week_in_month}"

        weekly_forecast.append({
            "week": week_idx + 1,
            "label": label,
            "forecast_date": forecast_date.strftime("%Y-%m-%d"),
            "predicted_units": pred
        })

        features = _update_forecast_features(features, pred, week_idx)

    return weekly_forecast


def _update_forecast_features(features, weekly_pred, week_idx):

    f = features.copy()

    # minimal drift only (prevents model drift over long forecasts)
    drift = min(week_idx * 0.015, 0.12)

    f['lag_7'] = (f['lag_7'] * (1 - drift)) + ((weekly_pred / 7) * drift)
    f['lag_14'] = (f['lag_14'] * (1 - drift)) + (f['lag_7'] * drift)

    f['rolling_mean_7'] = (f['rolling_mean_7'] * 0.95) + ((weekly_pred / 7) * 0.05)
    f['rolling_mean_30'] = (f['rolling_mean_30'] * 0.98) + ((weekly_pred / 7) * 0.02)

    f['trend_direction'] = f['rolling_mean_7'] - f['rolling_mean_30']

    return f   
        
        

def update_features_for_next_week(f, weekly_sales):

    new_f=f.copy()

    daily_avg=weekly_sales/7

    new_f['lag_30']=new_f.get('lag_14', 0)
    new_f['lag_14']=new_f.get('lag_7', 0)
    new_f['lag_7']=daily_avg

    new_f['rolling_mean_7']=daily_avg

    new_f['rolling_mean_30']=((new_f.get('rolling_mean_30', 0)*23)+ (daily_avg*7))/30

    new_f['rolling_mean_90']=((new_f.get('rolling_mean_90', 0)*83)+ (daily_avg*7))/90

    raw_trend=(new_f['lag_7']- new_f['lag_30'])

    new_f['trend_direction']=max(
        min(raw_trend, 5.0),
        -5.0
    )
    new_f['day_of_week']=(new_f.get('day_of_week', 0)+7) % 7

    new_f['is_weekend']=(1 if new_f['day_of_week'] >= 5 else 0)

    new_f['month']=(new_f.get('month', 1)+0.25)

    if new_f['month'] > 12.75:
        new_f['month']=1

    new_f['quarter']=((int(new_f['month'])-1) // 3)+1

    return new_f

@app.post("/simulate_demand_spike")
def simulate_demand_spike(data: dict = Body(...)):

    
    try:
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        product_code = data.get("product_code")
        spike_percent = float(data.get("spike_percent", 50))
        spike_duration_weeks = int(data.get("spike_duration_weeks", 2))

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()
        df = pd.DataFrame(Salesdata)

        if df.empty:
            raise HTTPException(status_code=500, detail="No sales data found")

        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        latest_df = (
            df.sort_values("sale_date")
              .groupby("item_id")
              .last()
              .reset_index()
        )

        item_id = str(product_code).replace("P", "").lstrip("0")
        row = latest_df[latest_df["item_id"] == item_id]

        if row.empty:
            raise HTTPException(
                status_code=404,
                detail="Product sales history not found"
            )

        row = row.iloc[0]
        feature_dict = {feat: float(row[feat]) for feat in FEATURES}

        product = next(
            (p for p in Products if p.get("product_code") == product_code),
            None
        )

        product_name = product.get("product_name") if product else product_code
        product_id = product.get("product_code") if product else None

        num_weeks = 6
        multiplier = 1 + (spike_percent / 100)

        baseline = []
        spike = []

        for week_idx in range(num_weeks):
            normal_pred = _predict_demand(feature_dict, _model, _scaler)

            baseline.append({
                "week": week_idx + 1,
                "predicted_units": normal_pred
            })

            if week_idx < spike_duration_weeks:
                spike_pred = round(normal_pred * multiplier)
            else:
                spike_pred = normal_pred

            spike.append({
                "week": week_idx + 1,
                "predicted_units": spike_pred
            })

        baseline_total = sum(x["predicted_units"] for x in baseline)
        spike_total = sum(x["predicted_units"] for x in spike)

        raw_material_impact = _calculate_bom_impact(
            product_id,
            RawMaterials,
            baseline_total,
            spike_total
        )

        return {
            "product_code": product_code,
            "product_name": product_name,
            "spike_percent": spike_percent,
            "spike_duration_weeks": spike_duration_weeks,
            "baseline_total": baseline_total,
            "spike_total": spike_total,
            "extra_demand": spike_total - baseline_total,
            "baseline_forecast": baseline,
            "spike_forecast": spike,
            "raw_material_impact": raw_material_impact
        }

    except Exception as e:
        print(f"Demand spike simulation failed for {product_code}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _predict_demand(features, model, scaler):

    X = np.array([[features[f] for f in FEATURES]], dtype=np.float32)

    if scaler:
        X = scaler.transform(X)

    prediction = float(model.predict(X)[0])
    return max(0, round(prediction))


def _calculate_bom_impact(product_id, raw_materials, baseline_total, spike_total):

    impact = []

    if not product_id:
        return impact

    bom = [
        m for m in raw_materials
        if str(m.get("product_id")) == str(product_id)
    ]

    for material in bom:
        qty_per_unit = float(material.get("qty_per_unit") or 1)
        stock_qty = float(material.get("stock_qty") or 0)

        baseline_usage = baseline_total * qty_per_unit
        spike_usage = spike_total * qty_per_unit
        extra_usage = spike_usage - baseline_usage

        impact.append({
            "material_code": material.get("material_code"),
            "material_name": material.get("material_name"),
            "unit": material.get("unit"),
            "unit_cost": float(material.get("unit_cost") or 0),
            "qty_per_unit": qty_per_unit,
            "baseline_consumption": baseline_usage,
            "spike_consumption": spike_usage,
            "extra_consumption": extra_usage,
            "current_stock": stock_qty,
            "stock_risk": stock_qty - spike_usage
        })

    return impact
@app.get("/get-products")
def get_products():

    try:
        Products=[]
        with engine.connect() as conn:
            Products= [dict(r) for r in conn.execute(text("SELECT*FROM products")).mappings().all()]

        if not Products:
            return []
        return Products

    except Exception as e:

        #print("Get Products Error:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to load products"
        )
@app.get("/get-sales")
def get_sales():

    try:
        sales=[]
        with engine.connect() as conn:
            sales= [dict(r) for r in conn.execute(text("SELECT id,sale_date,item_id,sales FROM sales_data")).mappings().all()]

        if not sales:
            return []
        return sales

    except Exception as e:

        #print("Get sales Error:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to load sales"
        )
        
@app.post("/simulate_supply_delay")
def simulate_supply_delay(data: dict = Body(...)):

    
    product_code = data.get("product_code")
    current_stock = int(data.get("current_stock", 0))
    restock_qty = int(data.get("restock_qty", 0))
    delay_weeks = int(data.get("delay_weeks", 0))

    try:
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        Products = _fetch_products()
        Salesdata = _fetch_sales_data()
        RawMaterials = _fetch_raw_materials()

        df = pd.DataFrame(Salesdata)
        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        item_id = product_code.replace("P", "").lstrip("0")
        row = latest_df[latest_df['item_id'] == item_id]

        if row.empty:
            raise HTTPException(
                status_code=404,
                detail="Product sales history not found"
            )

        row = row.iloc[0]
        current_features = {feat: float(row[feat]) for feat in FEATURES}

        product = next(
            (p for p in Products if p.get("product_code") == product_code),
            {}
        )
        product_name = product.get("product_name", product_code)
        product_id = product.get("product_code")

        num_weeks = 8
        arrival_week = 2 + delay_weeks
        inventory = current_stock

        weekly_forecast = []
        total_shortage = 0
        stockout_weeks = 0

        for week in range(1, num_weeks + 1):
            X = np.array([[
                current_features[f] for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X = _scaler.transform(X)

            predicted_demand = max(
                0,
                round(float(_model.predict(X)[0]))
            )

            inventory_before = inventory

            if week == arrival_week:
                inventory += restock_qty

            actual_sales = min(inventory, predicted_demand)
            shortage = max(0, predicted_demand - inventory)
            inventory = max(0, inventory - predicted_demand)

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
                "restock_arrived": week == arrival_week
            })

            current_features = update_features_for_next_week(
                current_features,
                predicted_demand
            )

        baseline_total = sum(w["predicted_demand"] for w in weekly_forecast)
        raw_material_impact = _calculate_bom_impact_suply_delay(
            product_id,
            RawMaterials,
            baseline_total
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
            "weekly_forecast": weekly_forecast,
            "raw_material_impact": raw_material_impact
        }

    except Exception as e:
        print(f"Supply delay simulation failed for {product_code}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _fetch_products():

    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(
                text("SELECT * FROM products")
            ).mappings().all()
        ]


def _fetch_sales_data():

    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(
                text("SELECT * FROM sales_data")
            ).mappings().all()
        ]


def _fetch_raw_materials():

    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(
                text("SELECT * FROM raw_materials")
            ).mappings().all()
        ]


def _calculate_bom_impact_suply_delay(product_id, raw_materials, total_demand):

    impact = []

    if not product_id:
        return impact

    bom = [
        m for m in raw_materials
        if str(m.get("product_id")) == str(product_id)
    ]

    for material in bom:
        qty_per_unit = float(material.get("qty_per_unit") or 1)
        stock_qty = float(material.get("stock_qty") or 0)
        required_qty = total_demand * qty_per_unit
        shortage_qty = max(0, required_qty - stock_qty)

        impact.append({
            "material_code": material.get("material_code"),
            "material_name": material.get("material_name"),
            "unit": material.get("unit"),
            "qty_per_unit": qty_per_unit,
            "required_qty": required_qty,
            "available_stock": stock_qty,
            "shortage": shortage_qty,
            "stock_risk": stock_qty - required_qty
        })

    return impact

@app.post("/simulate_seasonal_surge")
def simulate_seasonal_surge(data: dict = Body(...)):
    
    product_code = data.get("product_code")
    current_stock = int(data.get("current_stock", 1500))
    season_name = data.get("season_name", "Festival")
    peak_week = int(data.get("peak_week", 3))
    peak_boost_percent = float(data.get("peak_boost_percent", 120))
    season_duration_weeks = int(data.get("season_duration_weeks", 6))

    try:
        # model availability check
        if _model is None:
            raise HTTPException(status_code=503, detail="Model not loaded")

        Products, Boxes, Shelves, Racks, RawMaterials, Salesdata = load_data()
        df = pd.DataFrame(Salesdata)

        if df.empty:
            raise HTTPException(status_code=500, detail="No sales data found")

        # prepare sales data
        df['sale_date'] = pd.to_datetime(df['sale_date'])
        df['item_id'] = df['item_id'].astype(str)

        for col in FEATURES:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        # using latest row only for faster prediction
        latest_df = (
            df.sort_values('sale_date')
              .groupby('item_id')
              .last()
              .reset_index()
        )

        # normalize product code (P001 -> 1)
        item_id = product_code.replace("P", "").lstrip("0")
        row = latest_df[latest_df['item_id'] == item_id]

        if row.empty:
            raise HTTPException(
                status_code=404,
                detail="Product sales history not found"
            )

        row = row.iloc[0]

        # initialize feature sets for both scenarios
        current_features_normal = {
            feat: float(row[feat]) for feat in FEATURES
        }
        current_features_surge = {
            feat: float(row[feat]) for feat in FEATURES
        }

        # product metadata
        product = next(
            (p for p in Products if p.get("product_code") == product_code),
            {}
        )
        product_name = product.get("product_name", product_code)

        # calculate weekly demand multipliers
        # ramps up to peak_week, then decays back down
        multipliers = _calculate_seasonal_multipliers(
            peak_week,
            season_duration_weeks,
            peak_boost_percent
        )

        # run dual-scenario forecast
        weekly_forecast = []
        stock_normal = current_stock
        stock_surge = current_stock
        stockout_week = None

        for week_idx in range(season_duration_weeks):
            # predict normal scenario
            normal_pred = _predict_demand(
                current_features_normal,
                _model,
                _scaler
            )

            # predict surge scenario (apply multiplier)
            surge_base = _predict_demand(
                current_features_surge,
                _model,
                _scaler
            )
            surge_pred = round(surge_base * multipliers[week_idx])

            # update stock levels
            stock_before_normal = stock_normal
            stock_before_surge = stock_surge

            stock_normal = max(0, stock_normal - normal_pred)
            stock_surge = max(0, stock_surge - surge_pred)

            # track when surge scenario runs out
            if stock_surge <= 0 and stockout_week is None:
                stockout_week = week_idx + 1

            # record weekly snapshot
            weekly_forecast.append({
                "week": week_idx + 1,
                "multiplier": multipliers[week_idx],
                "normal_prediction": normal_pred,
                "surge_prediction": surge_pred,
                "normal_stock_before": stock_before_normal,
                "normal_stock_after": stock_normal,
                "surge_stock_before": stock_before_surge,
                "surge_stock_after": stock_surge
            })

            # update features for next iteration
            current_features_normal = update_features_for_next_week(
                current_features_normal,
                normal_pred
            )
            current_features_surge = update_features_for_next_week(
                current_features_surge,
                surge_pred
            )

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
        print(f"Seasonal surge simulation failed for {product_code}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _calculate_seasonal_multipliers(peak_week, duration, peak_percent):
    multipliers = []

    for week_num in range(1, duration + 1):
        if week_num <= peak_week:
            # linear ramp up to peak
            progress = week_num / peak_week
            boost = (peak_percent / 100) * progress
        else:
            # decay after peak
            weeks_after_peak = duration - peak_week
            progress = (week_num - peak_week) / max(1, weeks_after_peak)
            boost = (peak_percent / 100) * (1 - progress)

        multipliers.append(round(1 + boost, 2))

    return multipliers


def _predict_demand(features, model, scaler):
    X = np.array([[features[f] for f in FEATURES]], dtype=np.float32)
    if scaler:
        X = scaler.transform(X)

    prediction = float(model.predict(X)[0])
    return max(0, round(prediction))

@app.post("/simulate_new_product_launch")
def simulate_new_product_launch(data: dict = Body(...)):

    
    product_name = data.get("product_name")
    category = data.get("category", "Electronics")
    launch_week_month = int(data.get("launch_week_month", 3))
    initial_stock = int(data.get("initial_stock", 500))
    growth_rate_percent = float(data.get("growth_rate_percent", 15))
    num_weeks = int(data.get("num_weeks", 8))

    try:

        # derived from historical sales data
        category_baselines = {
            'Electronics': {'daily_avg': 35, 'volatility': 0.25},
            'Pumps': {'daily_avg': 80, 'volatility': 0.15},
            'Mechanical': {'daily_avg': 20, 'volatility': 0.10},
            'Machinery': {'daily_avg': 12, 'volatility': 0.30}
        }

        baseline = category_baselines.get(
            category,
            category_baselines['Electronics']
        )

        month_seasonality = {
            1: 0.70, 2: 0.65, 3: 0.75, 4: 0.80, 5: 0.85, 6: 0.90,
            7: 0.92, 8: 1.00, 9: 1.05, 10: 1.20, 11: 1.40, 12: 1.60
        }


        ramp_preds = []
        confidence = []
        current_month = launch_week_month
        base_daily = baseline['daily_avg'] * 0.30  # conservative first-week baseline

        for week_idx in range(num_weeks):
            if week_idx > 0:

                base_daily *= (1 + (growth_rate_percent / 100))

            base_daily = min(base_daily, baseline['daily_avg'] * 1.10)

            seasonal_mod = month_seasonality.get(current_month, 1.0)
            weekly_demand = round(base_daily * 7 * seasonal_mod)

            ramp_preds.append(weekly_demand)

            conf = min(85, 35 + (week_idx * 7))
            confidence.append(conf)

            if (week_idx + 1) % 4 == 0:
                current_month = current_month + 1 if current_month < 12 else 1

        # phase 2: blend model predictions for later weeks
        week4_daily = (
            ramp_preds[3] / 7 if len(ramp_preds) >= 4 else base_daily
        )

        # prepare feature set for ML model
        model_features = {
            'day_of_week': 0,
            'month': launch_week_month,
            'quarter': ((launch_week_month - 1) // 3) + 1,
            'is_weekend': 0,
            'is_month_start': 0,
            'is_month_end': 0,
            'lag_7': week4_daily,
            'lag_14': week4_daily * 0.85,
            'lag_30': week4_daily * 0.65,
            'lag_365': 0,
            'rolling_mean_7': week4_daily * 0.95,
            'rolling_mean_30': week4_daily * 0.70,
            'rolling_mean_90': week4_daily * 0.60,
            'rolling_std_7': week4_daily * baseline['volatility'],
            'trend_direction': week4_daily * 0.35,
            'yoy_growth': 0
        }

        # blend organic ramp with model predictions from week 4 onward
        for week_idx in range(4, num_weeks):
            X = np.array([[
                model_features[f] for f in FEATURES
            ]], dtype=np.float32)

            if _scaler:
                X = _scaler.transform(X)

            model_prediction = max(
                0,
                round(float(_model.predict(X)[0]))
            )
            blend_weight = min(1.0, (week_idx - 3) * 0.25)

            blended = round(
                (ramp_preds[week_idx] * (1 - blend_weight)) +
                (model_prediction * blend_weight)
            )

            ramp_preds[week_idx] = blended

        stock_levels = [initial_stock]
        restock_events = []
        weekly_forecast = []

        for week_idx in range(num_weeks):
            stock_before = stock_levels[-1]
            remaining = stock_before - ramp_preds[week_idx]

            restocked = False
            restock_qty = 0
            if remaining < (initial_stock * 0.20):
                restock_qty = initial_stock
                remaining += restock_qty
                restocked = True

                restock_events.append({
                    'week': week_idx + 1,
                    'qty': restock_qty
                })

            stock_after = max(0, remaining)
            stock_levels.append(stock_after)

            weekly_forecast.append({
                "week": week_idx + 1,
                "predicted_demand": ramp_preds[week_idx],
                "confidence": confidence[week_idx],
                "stock_before": stock_before,
                "stock_after": stock_after,
                "restocked": restocked,
                "restock_qty": restock_qty
            })

        return {
            "product_name": product_name,
            "category": category,
            "launch_week_month": launch_week_month,
            "growth_rate_percent": growth_rate_percent,
            "initial_stock": initial_stock,
            "weekly_forecast": weekly_forecast
        }

    except Exception as e:
        print(f"Launch simulation failed for {product_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))