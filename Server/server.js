import express from 'express';
import cors from 'cors';
import 'dotenv/config';
import mysql from 'mysql2/promise';
import path from "path";
import { fileURLToPath } from "url";
import jwt from 'jsonwebtoken';
import multer from "multer";
import ExcelJS from "exceljs";
import cookieParser from 'cookie-parser';
import axios from 'axios';

import { 
  setupDatabase,
  createProduct, 
  getProducts ,
  createShelves, 
  getShelves,
  createBoxes, 
  getBoxes,
  createRack, 
  getRacks,
} from './database_actions.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app=express();
const port=process.env.PORT ||  8080
app.use(express.json())
app.use(cookieParser())
app.use(cors({
    origin: process.env.DOMAIN,
    credentials:true}))
  const pool = mysql.createPool({
    host: 'localhost',
    user: process.env.DATABASE_USER,
    password: process.env.DATABASE_PASSWORD,
    database: process.env.DATABASE_NAME, 
    port: process.env.DATABASE_PORT,
    waitForConnections: true,
    connectionLimit: 10
  });
  const db = async (sql, params = []) => {
    const [rows] = await pool.execute(sql, params);
    return rows;
  };
   

app.get('/',async (req,res)=>{
  try {
    const response = await axios.get("http://127.0.0.1:8000/");
    res.json(response.data);
  } catch (error) {
      res.status(500).send("Error calling FastAPI");
  }
})
app.get('/app',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/app.html"))
})
app.get('/setup',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/setup.html"))
})


app.get('/stimulations',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/stimulations_c.html"))
})
// POST /create-rack
app.post("/create-rack", async (req, res) => {
  try {
    const { rack_code, type, created_by = 1 } = req.body;
    if (!rack_code || !type) return res.status(400).json({ error: "rack_code and type required" });
 
    const result = await db(
      `INSERT INTO racks (rack_code, type, created_by) VALUES (?, ?, ?)`,
      [rack_code, type, created_by]
    );
    res.json({ id: result.insertId, rack_code, type });
  } catch (e) {
    if (e.code === "ER_DUP_ENTRY") return res.status(409).json({ error: "Rack code already exists" });
    res.status(500).json({ error: e.message });
  }
});
 
// GET /get-racks
app.get("/get-racks", async (req, res) => {
  const rows = await db(`SELECT id, rack_code, type, created_by FROM racks ORDER BY id ASC`);
  res.json(rows);
});
 
// ============================================================
//  SHELVES
// ============================================================
 
// POST /create-shelves  (creates N levels under a rack)
app.post("/create-shelves", async (req, res) => {
  try {
    const { rack_id, levels, created_by = 1 } = req.body;
    if (!rack_id || !levels) return res.status(400).json({ error: "rack_id and levels required" });
 
    // Fetch rack code to build shelf codes
    const [rack] = await db(`SELECT rack_code FROM racks WHERE id = ?`, [rack_id]);
    if (!rack) return res.status(404).json({ error: "Rack not found" });
 
    const created = [];
    for (let i = 1; i <= +levels; i++) {
      const shelf_code = `${rack.rack_code}-SH${String(i).padStart(2, "0")}`;
      const r = await db(
        `INSERT IGNORE INTO shelves (shelf_code, rack_id, level, created_by) VALUES (?, ?, ?, ?)`,
        [shelf_code, rack_id, i, created_by]
      );
      created.push({ shelf_code, level: i, id: r.insertId });
    }
    res.json({ inserted: created.length, shelves: created });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
 
// GET /get-shelves
app.get("/get-shelves", async (req, res) => {
  const rows = await db(
    `SELECT s.id, s.shelf_code, s.rack_id, s.level, r.rack_code
     FROM shelves s JOIN racks r ON r.id = s.rack_id
     ORDER BY s.rack_id, s.level`
  );
  res.json(rows);
});
 
// ============================================================
//  BOXES
// ============================================================
 
// POST /create-boxes  (creates N boxes on a shelf)
app.post("/create-boxes", async (req, res) => {
  try {
    const { shelf_id, count, max_units, created_by = 1 } = req.body;
    if (!shelf_id || !count || !max_units)
      return res.status(400).json({ error: "shelf_id, count, max_units required" });
 
    const [shelf] = await db(`SELECT shelf_code FROM shelves WHERE id = ?`, [shelf_id]);
    if (!shelf) return res.status(404).json({ error: "Shelf not found" });
 
    // Find existing box count for this shelf to continue numbering
    const [{ existing }] = await db(
      `SELECT COUNT(*) AS existing FROM boxes WHERE shelf_id = ?`,
      [shelf_id]
    );
    const created = [];
    for (let i = 1; i <= +count; i++) {
      const boxNum = +existing + i;
      const box_code = `${shelf.shelf_code}-BX${String(boxNum).padStart(2, "0")}`;
      const r = await db(
        `INSERT INTO boxes (box_code, shelf_id, max_units, created_by) VALUES (?, ?, ?, ?)`,
        [box_code, shelf_id, max_units, created_by]
      );
      created.push({ box_code, id: r.insertId });
    }
    res.json({ inserted: created.length, boxes: created });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
 
// GET /get-boxes
app.get("/get-boxes", async (req, res) => {
  const rows = await db(
    `SELECT b.id, b.box_code, b.shelf_id, b.max_units, s.shelf_code
     FROM boxes b JOIN shelves s ON s.id = b.shelf_id
     ORDER BY b.shelf_id, b.id`
  );
  res.json(rows);
});
 
// ============================================================
//  PRODUCTS
// ============================================================
 
// POST /create-product
app.post("/create-product", async (req, res) => {
  try {
    const {
      product_code, product_name, category,
      unit_price, mfg_cost,
      stock_qty, reorder_level,
      daily_consumption = 0, demand = 0,
      size_category, box_id, created_by = 1,
    } = req.body;
 
    if (!product_code || !product_name)
      return res.status(400).json({ error: "product_code and product_name required" });
 
    const r = await db(
      `INSERT INTO products
        (product_code, product_name, category, unit_price, mfg_cost,
         stock_qty, reorder_level, daily_consumption, size_category,
         demand, box_id, created_by)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        product_code, product_name, category,
        unit_price, mfg_cost,
        stock_qty, reorder_level, daily_consumption,
        size_category, demand, box_id, created_by,
      ]
    );
    res.json({ id: r.insertId, product_code, product_name });
  } catch (e) {
    if (e.code === "ER_DUP_ENTRY")
      return res.status(409).json({ error: "Product code already exists" });
    res.status(500).json({ error: e.message });
  }
});
 
// POST /import-products  (bulk Excel import)
app.post("/import-products", async (req, res) => {
  try {
    const { products } = req.body;
    if (!Array.isArray(products) || !products.length)
      return res.status(400).json({ error: "products array is required" });
 
    let inserted = 0;
    let skipped = 0;
 
    for (const p of products) {
      try {
        await db(
          `INSERT INTO products
            (product_code, product_name, category, unit_price, mfg_cost,
             stock_qty, reorder_level, daily_consumption, size_category,
             demand, box_id, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            p.product_code, p.product_name, p.category || "",
            p.unit_price || 0, p.mfg_cost || 0,
            p.stock_qty || 0, p.reorder_level || 0,
            p.daily_consumption || 0, p.size_category || "medium",
            p.demand || 0, p.box_id || null, p.created_by || 1,
          ]
        );
        inserted++;
      } catch (e) {
        // Skip duplicates silently
        if (e.code === "ER_DUP_ENTRY") skipped++;
        else throw e;
      }
    }
    res.json({ inserted, skipped });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
 
app.get("/get_all_products", async (req, res) => {
  const sql='SELECT * from products';
  const rows = await db(sql);
  res.json(rows);
})

// GET /get-products
app.get("/get-products", async (req, res) => {
  const { category, size_category, low_stock } = req.query;
 
  let sql = `
    SELECT p.*, b.box_code
    FROM products p
    LEFT JOIN boxes b ON b.id = p.box_id
    WHERE 1=1`;
  const params = [];
 
  if (category) { sql += ` AND p.category = ?`; params.push(category); }
  if (size_category) { sql += ` AND p.size_category = ?`; params.push(size_category); }
  if (low_stock === "1") { sql += ` AND p.stock_qty <= p.reorder_level`; }
 
  sql += ` ORDER BY p.id DESC`;
  const rows = await db(sql, params);
  res.json(rows);
});
 
// GET /get-product/:id
app.get("/get-product/:id", async (req, res) => {
  const [row] = await db(
    `SELECT p.*, b.box_code FROM products p LEFT JOIN boxes b ON b.id = p.box_id WHERE p.id = ?`,
    [req.params.id]
  );
  if (!row) return res.status(404).json({ error: "Not found" });
  res.json(row);
});
 
// PATCH /update-product/:id
app.patch("/update-product/:id", async (req, res) => {
  const fields = ["category","unit_price","mfg_cost","stock_qty","reorder_level","size_category","demand","box_id"];
  const updates = [];
  const values = [];
  for (const f of fields) {
    if (req.body[f] !== undefined) {
      updates.push(`${f} = ?`);
      values.push(req.body[f]);
    }
  }
  if (!updates.length) return res.status(400).json({ error: "No fields to update" });
  values.push(req.params.id);
  await db(`UPDATE products SET ${updates.join(", ")}, updated_at = NOW() WHERE id = ?`, values);
  res.json({ updated: true });
});
 
// ============================================================
//  SALES DATA
// ============================================================
 
app.get("/get-sales", async (req, res) => {
  const { group_by = "item", from, to } = req.query;
  const allowed = ["item", "date"];
  const col = allowed.includes(group_by) ? group_by : "item";
 
  let sql = `SELECT ${col}, SUM(sales) AS total_sales, COUNT(*) AS records FROM sales_data WHERE 1=1`;
  const params = [];
  if (from) { sql += ` AND date >= ?`; params.push(from); }
  if (to)   { sql += ` AND date <= ?`; params.push(to); }
  sql += ` GROUP BY ${col} ORDER BY total_sales DESC`;
 
  const rows = await db(sql, params);
  res.json(rows);
});

app.post("/import-sales", async (req, res) => {
  try {
    const { records } = req.body;
    if (!Array.isArray(records) || !records.length)
      return res.status(400).json({ error: "records array required" });

    /**
     * Helper: Standardizes dates to YYYY-MM-DD.
     * Handles: '31-12-2023', '12/31/2023', JS Date objects, and ISO strings.
     */
    const parseDate = (d) => {
      if (!d) return null;
      let dateObj;

      if (typeof d === "string") {
        // Handle common DD-MM-YYYY or DD/MM/YYYY formats
        const parts = d.split(/[-/ ]/);
        if (parts.length === 3) {
          // If year is the first part (YYYY-MM-DD)
          if (parts[0].length === 4) {
            dateObj = new Date(parts[0], parts[1] - 1, parts[2]);
          } 
          // If year is the last part (DD-MM-YYYY)
          else if (parts[2].length === 4) {
            dateObj = new Date(parts[2], parts[1] - 1, parts[0]);
          }
        }
      }
      
      // Fallback to native constructor if parsing failed or format was different
      if (!dateObj || isNaN(dateObj)) dateObj = new Date(d);

      if (isNaN(dateObj.getTime())) return null;

      // Extract YYYY-MM-DD without timezone shifts
      const y = dateObj.getFullYear();
      const m = String(dateObj.getMonth() + 1).padStart(2, "0");
      const day = String(dateObj.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    };

    const rows = records
      .map(r => ({
        sale_date: parseDate(r.sale_date || r.date),
        item_id:   String(r.item_id || r.item),
        sales:     parseFloat(r.sales_qty ?? r.sales ?? 0),
      }))
      .filter(r => r.sale_date &&  r.item_id && !isNaN(r.sales))
      .sort((a, b) =>
        a.item_id.localeCompare(b.item_id) ||
        a.sale_date.localeCompare(b.sale_date)
      );

    if (!rows.length) return res.json({ inserted: 0, skipped: records.length });

    const groups = {};
    rows.forEach((r, i) => {
      const key = `${r.item_id}`;
      if (!groups[key]) groups[key] = [];
      groups[key].push(i);
    });

    // ── 3. Compute features per group ─────────────────────────
    const feat = rows.map(r => {
      // sale_date is now strictly YYYY-MM-DD string
      const [y, m, d] = r.sale_date.split("-").map(Number);
      const dateObj = new Date(y, m - 1, d);
      
      const day = dateObj.getDay(); // 0=Sun ... 6=Sat
      const dow = day === 0 ? 6 : day - 1; // Convert to 0=Mon ... 6=Sun
      
      return {
        day_of_week:    dow,
        month:          m,
        quarter:        Math.ceil(m / 3),
        is_weekend:     dow >= 5 ? 1 : 0,
        is_month_start: d <= 5 ? 1 : 0,
        is_month_end:   d >= 25 ? 1 : 0,
        lag_7: null, lag_14: null, lag_30: null, lag_365: null,
        rolling_mean_7: null, rolling_mean_30: null,
        rolling_mean_90: null, rolling_std_7: null,
        trend_direction: null, yoy_growth: null,
        sales_next_7: null,
      };
    });

    const shift = (vals, n) => {
      if (n > 0) return [...Array(n).fill(null), ...vals.slice(0, -n)];
      if (n < 0) return [...vals.slice(-n), ...Array(-n).fill(null)];
      return [...vals];
    };

    const rollingMean = (vals, w) => vals.map((_, i) => {
      if (i < w - 1) return null;
      const win = vals.slice(i - w + 1, i + 1);
      if (win.some(v => v == null)) return null;
      return win.reduce((s, v) => s + v, 0) / w;
    });

    const rollingStd = (vals, w) => vals.map((_, i) => {
      if (i < w - 1) return null;
      const win = vals.slice(i - w + 1, i + 1);
      if (win.some(v => v == null)) return null;
      const mean = win.reduce((s, v) => s + v, 0) / w;
      return Math.sqrt(win.reduce((s, v) => s + (v - mean) ** 2, 0) / w);
    });

    for (const indices of Object.values(groups)) {
      const sales = indices.map(i => rows[i].sales);

      const l7   = shift(sales, 7);
      const l14  = shift(sales, 14);
      const l30  = shift(sales, 30);
      const l365 = shift(sales, 365);

      const lag1      = shift(sales, 1);
      const rm7       = rollingMean(lag1, 7);
      const rm30      = rollingMean(lag1, 30);
      const rm90      = rollingMean(lag1, 90);
      const rs7       = rollingStd(lag1, 7);

      const shiftedFwd = shift(sales, -7);
      const sn7 = shiftedFwd.map((_, i) => {
        if (i < 6) return null;
        const win = shiftedFwd.slice(i - 6, i + 1);
        if (win.some(v => v == null)) return null;
        return win.reduce((s, v) => s + v, 0);
      });

      indices.forEach((rowIdx, j) => {
        const f = feat[rowIdx];
        f.lag_7           = l7[j];
        f.lag_14          = l14[j];
        f.lag_30          = l30[j];
        f.lag_365         = l365[j];
        f.rolling_mean_7  = rm7[j];
        f.rolling_mean_30 = rm30[j];
        f.rolling_mean_90 = rm90[j];
        f.rolling_std_7   = rs7[j];
        f.trend_direction = l7[j] != null && l30[j] != null ? l7[j] - l30[j] : null;
        f.yoy_growth      = l7[j] != null && l365[j] != null
          ? (l7[j] - l365[j]) / (l365[j] + 1) : null;
        f.sales_next_7    = sn7[j];
      });
    }

    // ── 4. Bulk INSERT IGNORE in chunks of 5000 ───────────────
    const CHUNK    = 5000;
    let inserted   = 0;
    let skipped    = 0;

    const COL_LIST = `
      sale_date,  item_id, sales,
      day_of_week, month, quarter,
      is_weekend, is_month_start, is_month_end,
      lag_7, lag_14, lag_30, lag_365,
      rolling_mean_7, rolling_mean_30, rolling_mean_90, rolling_std_7,
      trend_direction, yoy_growth, sales_next_7
    `;

    for (let start = 0; start < rows.length; start += CHUNK) {
      const chunk  = rows.slice(start, start + CHUNK);
      const ph     = chunk.map(() => "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").join(",");
      const values = [];

      chunk.forEach((r, i) => {
        const f = feat[start + i];
        values.push(
          r.sale_date,r.item_id, r.sales,
          f.day_of_week, f.month, f.quarter,
          f.is_weekend, f.is_month_start, f.is_month_end,
          f.lag_7, f.lag_14, f.lag_30, f.lag_365,
          f.rolling_mean_7, f.rolling_mean_30, f.rolling_mean_90, f.rolling_std_7,
          f.trend_direction, f.yoy_growth, f.sales_next_7
        );
      });

      const result = await db(
        `INSERT IGNORE INTO sales_data (${COL_LIST}) VALUES ${ph}`,
        values
      );
      inserted += result.affectedRows;
      skipped  += chunk.length - result.affectedRows;
    }

    res.json({
      total:    rows.length,
      inserted,
      skipped,
      ann_ready: rows.filter((_, i) => feat[i].sales_next_7 != null).length,
    });

  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});


// ─────────────────────────────────────────────────────────────
//  FIX 1: /sales-summary — was using non-existent column
//  `sales_qty`; correct column name is `sales`
// ─────────────────────────────────────────────────────────────
app.get("/sales-summary", async (req, res) => {
  const [rows] = await pool.execute(`
    SELECT
      COUNT(*)                          AS total_records,
      MIN(sale_date)                    AS earliest_date,
      MAX(sale_date)                    AS latest_date,
      DATEDIFF(MAX(sale_date), MIN(sale_date)) AS date_span_days,
      COUNT(DISTINCT item_id)           AS unique_items,
      SUM(sales)                        AS total_sales,
      ROUND(AVG(sales), 2)              AS avg_daily_sales
    FROM sales_data`);
  res.json(rows[0]);
});

 // ============================================================
//  RAW MATERIALS — add these routes to server.js
// ============================================================
// Helper: resolve rack+shelf+box strings → box_id
async function resolveBoxId(rack_code, shelf_level, box_code) {
  if (!rack_code || !shelf_level || !box_code) return null;
  const [rack] = await db(`SELECT id FROM racks WHERE rack_code = ?`, [rack_code]);
  if (!rack) return null;
  const [shelf] = await db(
    `SELECT id FROM shelves WHERE rack_id = ? AND level = ?`,
    [rack.id, shelf_level]
  );
  if (!shelf) return null;
  const [box] = await db(
    `SELECT id FROM boxes WHERE shelf_id = ? AND box_code = ?`,
    [shelf.id, box_code]
  );
  return box?.id || null;
}
 
// ─────────────────────────────────────────────────────────────
//  FIX 3: /import-products-with-location — VALUES clause had
//  only 11 `?` for 12 columns. Added missing `?` for
//  `created_by`.
// ─────────────────────────────────────────────────────────────
app.post("/import-products-with-location", async (req, res) => {
  try {
    const { products } = req.body;
    if (!Array.isArray(products) || !products.length)
      return res.status(400).json({ error: "products array required" });

    let inserted = 0, skipped = 0, unresolved = 0;

    for (const p of products) {
      // Normalize incoming strings — XLSX sometimes adds whitespace
      const rack_code   = (p.rack_code   || '').toString().trim();
      const box_code    = (p.box_code    || '').toString().trim();
      const shelf_level = +p.shelf_level || 0;

      console.log("🔍 Product location input:", { rack_code, shelf_level, box_code });

      const box_id = await ensureLocation(rack_code, shelf_level, box_code);

      console.log("📦 Resolved box_id:", box_id, "for product:", p.product_code);

      if (rack_code && !box_id) unresolved++;

      try {
        await db(
          `INSERT INTO products
            (product_code, product_name, category, unit_price, mfg_cost,
             stock_qty, reorder_level, daily_consumption, size_category,
             demand, box_id, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            (p.product_code   || '').toString().trim(),
            (p.product_name   || '').toString().trim(),
            (p.category       || '').toString().trim(),
            +p.unit_price     || 0,
            +p.mfg_cost       || 0,
            +p.stock_qty      || 0,
            +p.reorder_level  || 0,
            +p.daily_consumption || 0,
            p.size_category   || 'medium',
            +p.demand         || 0,
            box_id,
            null,             // created_by
          ]
        );
        inserted++;
      } catch (e) {
        if (e.code === 'ER_DUP_ENTRY') skipped++;
        else throw e;
      }
    }

    res.json({ inserted, skipped, unresolved });
  } catch (e) {
    console.error("❌ import-products-with-location error:", e);
    res.status(500).json({ error: e.message });
  }
});


// ─────────────────────────────────────────────────────────────
//  FIX 4: /import-raw-materials-with-location BOM block —
//  was fetching rm.id (numeric) and p.id (numeric) but
//  product_boms stores string codes, matching the JOIN in
//  /get-bom which uses rm.material_code and p.product_code.
//  Fixed to select rm.material_code and p.product_code.
// ─────────────────────────────────────────────────────────────
app.post("/import-raw-materials-with-location", async (req, res) => {
  try {
    const { materials } = req.body;

    if (!Array.isArray(materials) || !materials.length)
      return res.status(400).json({ error: "materials array required" });

    let inserted = 0, skipped = 0, unresolved = 0;

    for (const m of materials) {

      const box_id = await ensureLocation(
        m.rack_code || m.rack || '',
        +m.shelf_level || +m.shelf || 0,
        m.box_code || m.box_id || ''
      );

      if ((m.rack_code || m.rack) && !box_id) unresolved++;

      try {
        await db(
          `INSERT INTO raw_materials
            (material_code, material_name, category, unit, unit_cost,
             stock_qty, reorder_level, daily_consumption, size_category,
             lead_time_days, supplier_name, box_id, created_by, product_id, qty_per_unit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)`,
          [
            m.material_code || '',
            m.material_name || '',
            m.category || '',
            m.unit || '',
            +m.unit_cost || 0,
            +m.stock_qty || 0,
            +m.reorder_level || 0,
            +m.daily_consumption || 0,
            m.size_category || 'medium',
            +m.lead_time_days || 0,
            m.supplier_name || '',
            box_id,
            m.product_id || null,
            +m.qty_per_unit || 0
          ]
        );
        inserted++;
      } catch (e) {
        if (e.code === 'ER_DUP_ENTRY') skipped++;
        else throw e;
      }
    }

    // Use string codes (material_code, product_code) — not numeric IDs —
    // because product_boms joins on rm.material_code and p.product_code
    const materialsForBom = await db(`
      SELECT 
        rm.material_code AS material_id,
        p.product_code   AS product_id,
        rm.qty_per_unit
      FROM raw_materials rm
      JOIN products p ON p.product_code = rm.product_id
      WHERE rm.product_id IS NOT NULL 
        AND rm.qty_per_unit > 0
    `);

    let bomInserted = 0, bomSkipped = 0;

    for (const m of materialsForBom) {
      try {
        await db(`
          INSERT IGNORE INTO product_boms (product_id, material_id, qty_per_unit)
          VALUES (?, ?, ?)`, 
          [m.product_id, m.material_id, m.qty_per_unit]
        );
        bomInserted++;
      } catch (e) {
        if (e.code === "ER_DUP_ENTRY") bomSkipped++;
        else throw e;
      }
    }

    res.json({
      inserted,
      skipped,
      unresolved,
      bomInserted,
      bomSkipped
    });

  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post("/create-raw-material", async (req, res) => {
  try {
    const {
      material_code, material_name, category = "",
      unit = "", unit_cost = 0,
      stock_qty = 0, reorder_level = 0,
      daily_consumption = 0, size_category = "medium",
      lead_time_days = 0, supplier_name = "",
      box_id = null, created_by = null,
    } = req.body;

    if (!material_code || !material_name)
      return res.status(400).json({ error: "material_code and material_name required" });

    const r = await db(
      `INSERT INTO raw_materials
        (material_code, material_name, category, unit, unit_cost,
         stock_qty, reorder_level, daily_consumption, size_category,
         lead_time_days, supplier_name, box_id, created_by)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        material_code, material_name, category, unit, unit_cost,
        stock_qty, reorder_level, daily_consumption, size_category,
        lead_time_days, supplier_name, box_id, created_by
      ]
    );

    res.json({ id: r.insertId, material_code, material_name });

  } catch (e) {
    if (e.code === "ER_DUP_ENTRY")
      return res.status(409).json({ error: "Material code already exists" });

    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────────────────────────────────────────────
//  FIX 5: /auto-create-boms — was fetching p.id (numeric)
//  as product_id. product_boms.product_id must be the
//  product_code string to match the JOIN in /get-bom.
//  Fixed to select p.product_code AS product_id.
// ─────────────────────────────────────────────────────────────
app.post("/auto-create-boms", async (req, res) => {
  try {
    const materials = await db(`
      SELECT 
        rm.material_code, 
        p.product_code AS product_id,
        rm.qty_per_unit
      FROM raw_materials rm
      JOIN products p ON p.product_code = rm.product_id
      WHERE rm.product_id IS NOT NULL AND rm.qty_per_unit > 0
    `);

    if (!materials.length) {
      return res.json({ inserted: 0, skipped: 0, message: "No linked materials found" });
    }

    let inserted = 0, skipped = 0;

    for (const m of materials) {
      try {
        await db(
          `INSERT INTO product_boms (product_id, material_id, qty_per_unit)
           VALUES (?, ?, ?)
           ON DUPLICATE KEY UPDATE qty_per_unit = VALUES(qty_per_unit)`,
          [
            m.product_id,        // product_code string
            m.material_code,     // material_code string
            +m.qty_per_unit || 1
          ]
        );
        inserted++;
      } catch (e) {
        if (e.code === "ER_DUP_ENTRY") skipped++;
        else throw e;
      }
    }

    res.json({ inserted, skipped, total: materials.length });

  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get("/get-bom/:product_code", async (req, res) => {
  try {
    const rows = await db(`
      SELECT
        pb.qty_per_unit,
        rm.material_code, rm.material_name, rm.category,
        rm.unit, rm.unit_cost, rm.stock_qty, rm.reorder_level,
        rm.size_category, rm.supplier_name,
        b.box_code,
        FLOOR(rm.stock_qty / pb.qty_per_unit) AS producible_units
      FROM product_boms pb
      JOIN raw_materials rm ON rm.material_code = pb.material_id
      JOIN products p ON p.product_code = pb.product_id
      LEFT JOIN boxes b ON b.id = rm.box_id
      WHERE p.product_code = ?
      ORDER BY rm.category, rm.material_name
    `, [req.params.product_code]);

    if (!rows.length) return res.status(404).json({ error: "No BOM found" });

    const bottleneck = rows.reduce((min, r) =>
      r.producible_units < min.producible_units ? r : min
    );

    res.json({
      product_code: req.params.product_code,
      bom_lines: rows.length,
      max_producible_units: bottleneck.producible_units,
      bottleneck_material: bottleneck.material_name,
      bom: rows
    });

  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get("/get-bom-summary", async (req, res) => {
  try {
    const rows = await db(`
      SELECT
        p.product_code, p.product_name,
        COUNT(pb.id) AS total_materials,
        MIN(FLOOR(rm.stock_qty / pb.qty_per_unit)) AS max_producible_units,
        SUM(CASE WHEN rm.stock_qty <= rm.reorder_level THEN 1 ELSE 0 END) AS low_stock_materials
      FROM products p
      JOIN product_boms pb ON pb.product_id = p.product_code
      JOIN raw_materials rm ON rm.material_code = pb.material_id
      GROUP BY p.product_code, p.product_name
      ORDER BY max_producible_units ASC
    `);
    res.json(rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post("/import-raw-materials", async (req, res) => {
  try {
    const { materials } = req.body;
    if (!Array.isArray(materials) || !materials.length)
      return res.status(400).json({ error: "materials array required" });

    let inserted = 0, skipped = 0;

    for (const m of materials) {
      try {
        await db(
          `INSERT INTO raw_materials
            (material_code, material_name, category, unit, unit_cost,
             stock_qty, reorder_level, daily_consumption, size_category,
             lead_time_days, supplier_name, box_id, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)`,
          [
            m.material_code || "",
            m.material_name || "",
            m.category || "",
            m.unit || "",
            +m.unit_cost || 0,
            +m.stock_qty || 0,
            +m.reorder_level || 0,
            +m.daily_consumption || 0,
            m.size_category || "medium",
            +m.lead_time_days || 0,
            m.supplier_name || "",
            m.box_id || null
          ]
        );
        inserted++;
      } catch (e) {
        if (e.code === "ER_DUP_ENTRY") skipped++; else throw e;
      }
    }

    res.json({ inserted, skipped });

  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
 
app.get("/get-raw-materials", async (req, res) => {
  const { category, size_category, low_stock } = req.query;

  let sql = `
    SELECT
      m.id,
      m.material_code,
      m.material_name,
      m.category,
      m.unit,
      m.unit_cost,
      m.stock_qty,
      m.reorder_level,
      m.daily_consumption,
      m.size_category,
      m.lead_time_days,
      m.supplier_name,
      m.box_id,
      b.box_code,
      p.product_name,
      m.updated_at,
      IF(m.stock_qty <= m.reorder_level, 'Low', 'OK') AS stock_status
    FROM raw_materials m
    LEFT JOIN boxes b ON b.id = m.box_id
    LEFT JOIN products p ON p.product_code = m.product_id 
    WHERE 1=1
  `;

  const params = [];

  if (category) {
    sql += ` AND m.category = ?`;
    params.push(category);
  }

  if (size_category) {
    sql += ` AND m.size_category = ?`;
    params.push(size_category);
  }

  if (low_stock === "1") {
    sql += ` AND m.stock_qty <= m.reorder_level`;
  }

  sql += ` ORDER BY m.id DESC`;

  try {
    const rows = await db(sql, params);
    res.json(rows);
  } catch (error) {
    console.error("Error fetching raw materials:", error);
    res.status(500).json({ error: "Internal Server Error" });
  }
});

app.patch("/update-raw-material/:id", async (req, res) => {
  const allowed = [
    "category","unit","unit_cost","stock_qty","reorder_level",
    "daily_consumption","size_category","lead_time_days",
    "supplier_name","product_id","qty_per_unit","box_id"
  ];
  const updates = [], values = [];
  for (const f of allowed) {
    if (req.body[f] !== undefined) { updates.push(`${f} = ?`); values.push(req.body[f]); }
  }
  if (!updates.length) return res.status(400).json({ error: "No fields to update" });
  values.push(req.params.id);
  await db(
    `UPDATE raw_materials SET ${updates.join(", ")}, updated_at = NOW() WHERE id = ?`,
    values
  );
  res.json({ updated: true });
});
 
app.delete("/delete-raw-material/:id", async (req, res) => {
  await db(`DELETE FROM raw_materials WHERE id = ?`, [req.params.id]);
  res.json({ deleted: true });
});
 
app.get("/get-raw-materials-summary", async (_, res) => {
  const [row] = await db(`
    SELECT
      COUNT(*)                                                   AS total,
      COUNT(CASE WHEN stock_qty <= reorder_level THEN 1 END)     AS low_stock_count,
      ROUND(SUM(unit_cost * stock_qty), 2)                       AS total_value,
      COUNT(DISTINCT category)                                   AS categories,
      COUNT(DISTINCT supplier_name)                              AS suppliers,
      COUNT(CASE WHEN product_id IS NOT NULL THEN 1 END)         AS linked_to_product
    FROM raw_materials`);
  res.json(row);
});

// ─────────────────────────────────────────────────────────────
//  FIX 2: /sales-by-item — was selecting non-existent column
//  `sales_qty`; correct column name is `sales`
// ─────────────────────────────────────────────────────────────
app.get("/sales-by-item", async (req, res) => {
  const { item_id } = req.query;
  if (!item_id) return res.status(400).json({ error: "item_id required" });
 
  const sql = `
    SELECT sale_date, item_id, sales
    FROM sales_data WHERE item_id = ?
    ORDER BY sale_date`;
 
  const rows = await db(sql, [item_id]);
  res.json(rows);
});

app.get("/get-sales-summary", async (_, res) => {
  const [row] = await db(`
    SELECT COUNT(*)                          AS total_records,
           MIN(sale_date)                    AS earliest_date,
           MAX(sale_date)                    AS latest_date,
           DATEDIFF(MAX(sale_date), MIN(sale_date)) AS date_span_days,
           COUNT(DISTINCT item_id)           AS unique_items,
           SUM(sales)                        AS total_sales,
           COUNT(CASE WHEN sales_next_7 IS NOT NULL THEN 1 END) AS ann_ready_rows
    FROM sales_data`);
  res.json(row);
});
 

app.listen(port,()=>{
  console.log(`Server started on port ${port}`)
  setupDatabase()
  
})

// Helper: get-or-create rack → shelf → box, returns box_id
async function ensureLocation(rack_code, shelf_level, box_code) {
  try {
    if (!rack_code) return null;

    rack_code   = rack_code.toString().trim();
    shelf_level = Number(shelf_level);
    box_code    = (box_code || '').toString().trim();

    if (isNaN(shelf_level) || !box_code) {
      console.warn(`⚠️ Invalid shelf(${shelf_level}) or box(${box_code}) for rack ${rack_code}`);
      return null;
    }

    // ── 1. RACK ──
    const type = rack_code.startsWith('RK-M') ? 'raw_material' : 'product';
    
    await db(`INSERT IGNORE INTO racks (rack_code, type) VALUES (?, ?)`, [rack_code, type]);
    const rack_rows = await db(`SELECT id FROM racks WHERE rack_code = ?`, [rack_code]);
    const rack_id = rack_rows[0]?.id;

    // ── 2. SHELF ──
    const shelf_code = `${rack_code}-SH${String(shelf_level).padStart(2, '0')}`;
    await db(`INSERT IGNORE INTO shelves (shelf_code, rack_id, level, created_by) VALUES (?, ?, ?, NULL)`, [shelf_code, rack_id, shelf_level]);
    const shelf_rows = await db(`SELECT id FROM shelves WHERE rack_id = ? AND level = ?`, [rack_id, shelf_level]);
    const shelf_id = shelf_rows[0]?.id;
    if (!shelf_id) { console.error('❌ shelf_id not resolved for', shelf_code); return null; }

    // ── 3. BOX ──
    await db(`INSERT IGNORE INTO boxes (box_code, shelf_id, max_units, created_by) VALUES (?, ?, 50, NULL)`, [box_code, shelf_id]);
    const box_rows = await db(`SELECT id FROM boxes WHERE shelf_id = ? AND box_code = ?`, [shelf_id, box_code]);
    const box_id = box_rows[0]?.id;
    if (!box_id) { console.error('❌ box_id not resolved for', box_code); return null; }
    return box_id;

  } catch (err) {
    console.error('❌ ensureLocation error:', err);
    return null;
  }
}