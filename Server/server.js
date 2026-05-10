import express from 'express';
import cors from 'cors';
import 'dotenv/config';
import bcrypt from 'bcrypt';
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
app.get('/products',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/products.html"))
})
app.get('/login',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/login.html"))
})

app.post("/login-user", async (req, res) => {
    try {
        const { email, password } = req.body;

        const users = await db(
            `SELECT * FROM users WHERE email=?`,
            [email]
        );

        if (!users.length)
            return res.status(401).json({ error: "Invalid email" });

        const user = users[0];

        const valid = await bcrypt.compare(
            password,
            user.password
        );

        if (!valid)
            return res.status(401).json({ error: "Invalid password" });

        const token = jwt.sign(
            {
                id: user.id,
                email: user.email
            },
            process.env.JWT_SECRETKEY,
            { expiresIn: "7d" }
        );

        res.json({
            success: true,
            token,
            user: {
                id: user.id,
                name: user.name,
                email: user.email
            }
        });

    } catch (e) {
        console.error(e);
        res.status(500).json({ error: "Login failed" });
    }
});
app.get("/verify-user", async (req, res) => {
    try {
        const token = req.headers.authorization?.split(" ")[1];


        if (!token)
            return res.status(401).json({ success:false, error:"Token missing" });

        const decoded = jwt.verify(token, process.env.JWT_SECRETKEY);
        // console.log(decoded)

        const users = await db(
            `SELECT id,name,email FROM users WHERE email=?`,
            [decoded.email]
        );

        if (!users.length)
            return res.status(404).json({ success:false, error:"User not found" });

        res.json({
            success: true,
            user: users[0]
        });

    } catch (e) {
        res.status(401).json({ success:false, error:"Invalid token" });
    }
});

app.get('/stimulations',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/stimulations_c.html"))
})
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
 
app.get("/get-racks", async (req, res) => {
  const rows = await db(`SELECT id, rack_code, type, created_by FROM racks ORDER BY id ASC`);
  res.json(rows);
});
 
app.post("/create-shelves", async (req, res) => {
  try {
    const { rack_id, levels, created_by = 1 } = req.body;
    if (!rack_id || !levels) return res.status(400).json({ error: "rack_id and levels required" });
 
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
 
app.get("/get-shelves", async (req, res) => {
  const rows = await db(
    `SELECT s.id, s.shelf_code, s.rack_id, s.level, r.rack_code
     FROM shelves s JOIN racks r ON r.id = s.rack_id
     ORDER BY s.rack_id, s.level`
  );
  res.json(rows);
});
 
app.post("/create-boxes", async (req, res) => {
  try {
    const { shelf_id, count, max_units, created_by = 1 } = req.body;
    if (!shelf_id || !count || !max_units)
      return res.status(400).json({ error: "shelf_id, count, max_units required" });
 
    const [shelf] = await db(`SELECT shelf_code FROM shelves WHERE id = ?`, [shelf_id]);
    if (!shelf) return res.status(404).json({ error: "Shelf not found" });
 
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
 
app.get("/get-boxes", async (req, res) => {
  const rows = await db(
    `SELECT b.id, b.box_code, b.shelf_id, b.max_units, s.shelf_code
     FROM boxes b JOIN shelves s ON s.id = b.shelf_id
     ORDER BY b.shelf_id, b.id`
  );
  res.json(rows);
});
app.post("/create-single-raw-material", async (req, res) => {
    try {
        const {
            material_code,
            material_name,
            category = "",
            unit = "",
            unit_cost = 0,
            stock_qty = 0,
            reorder_level = 0,
            daily_consumption = 0,
            size_category = "medium",
            lead_time_days = 0,
            supplier_name = "",
            box_id = null,
            created_by = null
        } = req.body;
 
        // Validate required fields
        if (!material_code || !material_name) {
            return res.status(400).json({ 
                error: "material_code and material_name are required" 
            });
        }
 
        // Check if material code already exists
        const existing = await db(
            `SELECT id FROM raw_materials WHERE material_code = ?`,
            [material_code]
        );
 
        if (existing && existing.length > 0) {
            return res.status(409).json({ 
                error: `Material code '${material_code}' already exists` 
            });
        }
 
        // Insert raw material
        const result = await db(
            `INSERT INTO raw_materials 
            (material_code, material_name, category, unit, unit_cost, 
             stock_qty, reorder_level, daily_consumption, size_category, 
             lead_time_days, supplier_name, box_id, created_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())`,
            [
                material_code,
                material_name,
                category,
                unit,
                unit_cost,
                stock_qty,
                reorder_level,
                daily_consumption,
                size_category,
                lead_time_days,
                supplier_name,
                box_id,
                created_by
            ]
        );
 
        res.status(201).json({
            success: true,
            message: "Raw material created successfully",
            id: result.insertId,
            material_code,
            material_name
        });
 
    } catch (e) {
        console.error('Error creating raw material:', e);
        res.status(500).json({ 
            error: "Failed to create raw material", 
            message: e.message 
        });
    }
});
 
app.post("/create-single-product", async (req, res) => {
    try {
        const {
            product_code,
            product_name,
            category = "",
            unit_price = 0,
            mfg_cost = 0,
            stock_qty = 0,
            reorder_level = 0,
            daily_consumption = 0,
            size_category = "medium",
            demand = 0,
            box_id = null,
            created_by = null
        } = req.body;
 
        // Validate required fields
        if (!product_code || !product_name) {
            return res.status(400).json({ 
                error: "product_code and product_name are required" 
            });
        }
 
        // Check if product code already exists
        const existing = await db(
            `SELECT id FROM products WHERE product_code = ?`,
            [product_code]
        );
 
        if (existing && existing.length > 0) {
            return res.status(409).json({ 
                error: `Product code '${product_code}' already exists` 
            });
        }
 
        // Insert product
        const result = await db(
            `INSERT INTO products 
            (product_code, product_name, category, unit_price, mfg_cost, 
             stock_qty, reorder_level, daily_consumption, size_category, 
             demand, box_id, created_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())`,
            [
                product_code,
                product_name,
                category,
                unit_price,
                mfg_cost,
                stock_qty,
                reorder_level,
                daily_consumption,
                size_category,
                demand,
                box_id,
                created_by
            ]
        );
 
        res.status(201).json({
            success: true,
            message: "Product created successfully",
            id: result.insertId,
            product_code,
            product_name
        });
 
    } catch (e) {
        console.error('Error creating product:', e);
        res.status(500).json({ 
            error: "Failed to create product", 
            message: e.message 
        });
    }
});

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
 
app.get("/get-product/:id", async (req, res) => {
  const [row] = await db(
    `SELECT p.*, b.box_code FROM products p LEFT JOIN boxes b ON b.id = p.box_id WHERE p.id = ?`,
    [req.params.id]
  );
  if (!row) return res.status(404).json({ error: "Not found" });
  res.json(row);
});
 
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


app.get("/get-allsales", async (req, res) => {
    try {
        // Parse parameters
        const limit = Math.max(1, Math.min(parseInt(req.query.limit) || 200, 1000));
        const offset = Math.max(0, parseInt(req.query.offset) || 0);
        
        // console.log(`Fetching sales: limit=${limit}, offset=${offset}`);
        
        // Get total count (separate query)
        const countRows = await db(`SELECT COUNT(*) as total FROM sales_data`);
        const total = countRows[0]?.total || 0;
        
        // Fetch paginated data
        // NOTE: LIMIT and OFFSET must be integers, not parameterized
        const sql = `
            SELECT *
            FROM sales_data
            ORDER BY id DESC
            LIMIT ${limit} OFFSET ${offset}
        `;
        
        const rows = await db(sql);

        res.json({
            data: rows || [],
            total: total,
            limit: limit,
            offset: offset,
            hasMore: (offset + limit) < total
        });

    } catch (e) {
        console.error('Error fetching sales:', e);
        res.status(500).json({
            error: "Failed to fetch sales data",
            message: e.message
        });
    }
});
app.post("/import-sales", async (req, res) => {
  try {
    const { records } = req.body;
    if (!Array.isArray(records) || !records.length)
      return res.status(400).json({ error: "records array required" });

    const parseDate = (d) => {
      if (!d) return null;
      let dateObj;

      if (typeof d === "string") {
        const parts = d.split(/[-/ ]/);
        if (parts.length === 3) {
          if (parts[0].length === 4) {
            dateObj = new Date(parts[0], parts[1] - 1, parts[2]);
          } 
          else if (parts[2].length === 4) {
            dateObj = new Date(parts[2], parts[1] - 1, parts[0]);
          }
        }
      }
      
      if (!dateObj || isNaN(dateObj)) dateObj = new Date(d);

      if (isNaN(dateObj.getTime())) return null;

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

    const feat = rows.map(r => {
      const [y, m, d] = r.sale_date.split("-").map(Number);
      const dateObj = new Date(y, m - 1, d);
      
      const day = dateObj.getDay();
      const dow = day === 0 ? 6 : day - 1;
      
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
 
app.post("/import-products-with-location", async (req, res) => {
  try {
    const { products } = req.body;
    if (!Array.isArray(products) || !products.length)
      return res.status(400).json({ error: "products array required" });

    let inserted = 0, skipped = 0, unresolved = 0;

    for (const p of products) {
      const rack_code   = (p.rack_code   || '').toString().trim();
      const box_code    = (p.box_code    || '').toString().trim();
      const shelf_level = +p.shelf_level || 0;

      const box_id = await ensureLocation(rack_code, shelf_level, box_code);

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
            null,
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
    res.status(500).json({ error: e.message });
  }
});

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
            m.product_id,
            m.material_code,
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
 
// ML model apis

async function getOptimizedLayout() {
    try {
        const response = await axios.post('http://localhost:8000/optimizelayout'); 
        return response.data;
    } catch (error) {
        console.error('Error fetching optimized layout:', error.message);
        throw error;
    }
}
app.get('/optimize_layout', async (req, res) => {
    try {
      let fastapi_domain=process.env.fastapi_site_domain
        const response = await axios.post(fastapi_domain+"/optimizelayout");
        res.json(response.data);
    } catch (error) {
        console.error('FastAPI error:', error.message);
        res.status(500).json({ error: 'Failed to fetch layout suggestions' });
    }
});

app.get('/optimize_raw_materials', async (req, res) => {
    try {
      let fastapi_domain=process.env.fastapi_site_domain
        const response = await axios.post(fastapi_domain+"/optimize_raw_materials");
        res.json(response.data);
    } catch (error) {
        console.error('FastAPI error:', error.message);
        res.status(500).json({ error: 'Failed to fetch layout suggestions' });
    }
});


app.listen(3000, () => {
    console.log('Node server running on port 3000');
});




















app.listen(port,()=>{
  console.log(`Server started on port ${port}`)
  setupDatabase()
  
})

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

    const type = rack_code.startsWith('RK-M') ? 'raw_material' : 'product';
    
    await db(`INSERT IGNORE INTO racks (rack_code, type) VALUES (?, ?)`, [rack_code, type]);
    const rack_rows = await db(`SELECT id FROM racks WHERE rack_code = ?`, [rack_code]);
    const rack_id = rack_rows[0]?.id;

    const shelf_code = `${rack_code}-SH${String(shelf_level).padStart(2, '0')}`;
    await db(`INSERT IGNORE INTO shelves (shelf_code, rack_id, level, created_by) VALUES (?, ?, ?, NULL)`, [shelf_code, rack_id, shelf_level]);
    const shelf_rows = await db(`SELECT id FROM shelves WHERE rack_id = ? AND level = ?`, [rack_id, shelf_level]);
    const shelf_id = shelf_rows[0]?.id;
    if (!shelf_id) { console.error('shelf_id not resolved for', shelf_code); return null; }

    await db(`INSERT IGNORE INTO boxes (box_code, shelf_id, max_units, created_by) VALUES (?, ?, 50, NULL)`, [box_code, shelf_id]);
    const box_rows = await db(`SELECT id FROM boxes WHERE shelf_id = ? AND box_code = ?`, [shelf_id, box_code]);
    const box_id = box_rows[0]?.id;
    if (!box_id) { console.error('box_id not resolved for', box_code); return null; }
    return box_id;

  } catch (err) {
    console.error('ensureLocation error:', err);
    return null;
  }
}