import fs from 'fs/promises' // in modules this is the way to import
import { domainToUnicode } from 'url';
import bcrypt from 'bcrypt';
import jwt from 'jsonwebtoken';
import mysql from 'mysql2/promise'
import ExcelJS from 'exceljs/dist/es5/index.js';
// const ExcelJS = require('exceljs/dist/es5');



// database_actions.js — only the setupDatabase function needs changes

export async function setupDatabase() {
  try {
    const bootstrap = await mysql.createConnection({
      host:     process.env.DATABASE_HOST     || "localhost",
      user:     process.env.DATABASE_USER,
      password: process.env.DATABASE_PASSWORD,
      port:     +process.env.DATABASE_PORT    || 3306,
    });
    await bootstrap.query(`CREATE DATABASE IF NOT EXISTS warehouse_optimizer`);
    await bootstrap.end();

    const pool = mysql.createPool({
      host:               process.env.DATABASE_HOST     || "localhost",
      user:               process.env.DATABASE_USER,
      password:           process.env.DATABASE_PASSWORD,
      port:               +process.env.DATABASE_PORT    || 3306,
      database:           "warehouse_optimizer",
      waitForConnections: true,
      connectionLimit:    10,
    });

    const q = (sql) => pool.query(sql);

    await q(`
      CREATE TABLE IF NOT EXISTS users (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(255),
        email      VARCHAR(255) UNIQUE,
        role       VARCHAR(50),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS racks (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        rack_code  VARCHAR(50) UNIQUE,
        type       VARCHAR(50),
        created_by INT,
        FOREIGN KEY (created_by) REFERENCES users(id)
          ON DELETE SET NULL ON UPDATE CASCADE
      )
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS shelves (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        shelf_code VARCHAR(50) UNIQUE,
        rack_id    INT,
        level      INT,
        created_by INT,
        FOREIGN KEY (rack_id)    REFERENCES racks(id)  ON DELETE CASCADE,
        FOREIGN KEY (created_by) REFERENCES users(id)  ON DELETE SET NULL
      )
    `);

    await pool.query(`
      ALTER TABLE shelves
      ADD UNIQUE KEY IF NOT EXISTS uq_shelf_rack_level (rack_id, level)
    `).catch(() => {});

    await q(`
      CREATE TABLE IF NOT EXISTS boxes (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        box_code   VARCHAR(50) UNIQUE,
        shelf_id   INT,
        max_units  INT,
        created_by INT,
        FOREIGN KEY (shelf_id)   REFERENCES shelves(id) ON DELETE CASCADE,
        FOREIGN KEY (created_by) REFERENCES users(id)   ON DELETE SET NULL
      )
    `);

    await pool.query(`
      ALTER TABLE boxes
      ADD UNIQUE KEY IF NOT EXISTS uq_box_shelf_code (shelf_id, box_code)
    `).catch(() => {});

    await q(`
      CREATE TABLE IF NOT EXISTS products (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        product_code      VARCHAR(50) UNIQUE,
        product_name      VARCHAR(255),
        category          VARCHAR(100),
        unit_price        FLOAT DEFAULT 0,
        mfg_cost          FLOAT DEFAULT 0,
        stock_qty         FLOAT DEFAULT 0,
        reorder_level     FLOAT DEFAULT 0,
        daily_consumption FLOAT DEFAULT 0,
        size_category     VARCHAR(50) DEFAULT 'medium',
        demand            FLOAT DEFAULT 0,
        box_id            INT,
        created_by        INT,
        updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (box_id)     REFERENCES boxes(id) ON DELETE SET NULL,
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
      )
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS raw_materials (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        material_code     VARCHAR(50) UNIQUE,
        material_name     VARCHAR(255),
        category          VARCHAR(100),
        unit              VARCHAR(50),
        unit_cost         FLOAT DEFAULT 0,
        stock_qty         FLOAT DEFAULT 0,
        reorder_level     FLOAT DEFAULT 0,  
        daily_consumption FLOAT DEFAULT 0,
        size_category     VARCHAR(50) DEFAULT 'medium',
        lead_time_days    INT DEFAULT 0,
        supplier_name     VARCHAR(255),
        product_id        VARCHAR(50) DEFAULT NULL,
        qty_per_unit      FLOAT DEFAULT 0,
        box_id            INT,
        created_by        INT,
        updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products(product_code) ON DELETE SET NULL,
        FOREIGN KEY (box_id)     REFERENCES boxes(id)    ON DELETE SET NULL,
        FOREIGN KEY (created_by) REFERENCES users(id)    ON DELETE SET NULL
      )
    `);

    await pool.query(`ALTER TABLE raw_materials ADD COLUMN IF NOT EXISTS product_id VARCHAR(50) DEFAULT NULL`).catch(() => {});
    await pool.query(`ALTER TABLE raw_materials ADD COLUMN IF NOT EXISTS qty_per_unit FLOAT DEFAULT 0`).catch(() => {});
    await pool.query(`
      ALTER TABLE raw_materials
      ADD CONSTRAINT IF NOT EXISTS fk_rm_product
      FOREIGN KEY (product_id) REFERENCES products(product_code) ON DELETE SET NULL
    `).catch(() => {});

    await q(`
      CREATE TABLE IF NOT EXISTS ann_suggestions (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        item_type        VARCHAR(50),
        item_id          INT,
        current_box_id   INT,
        suggested_box_id INT,
        reorder_qty      FLOAT,
        priority_flag    INT,
        confidence_score FLOAT,
        generated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (current_box_id)   REFERENCES boxes(id) ON DELETE SET NULL,
        FOREIGN KEY (suggested_box_id) REFERENCES boxes(id) ON DELETE SET NULL
      )
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS product_boms (
                id INT AUTO_INCREMENT PRIMARY KEY,
        product_id INT NOT NULL,
        material_id INT NOT NULL,
        qty_per_unit FLOAT DEFAULT 0,

        FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
        FOREIGN KEY (material_id) REFERENCES raw_materials(id) ON DELETE CASCADE,

        UNIQUE KEY uq_product_material (product_id, material_id)
      )
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS sales_data (
        id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
        sale_date            DATE        NOT NULL,
        item_id              VARCHAR(50) NOT NULL,
        sales                FLOAT       NOT NULL DEFAULT 0,
        day_of_week          TINYINT,
        month                TINYINT,
        quarter              TINYINT,
        is_weekend           TINYINT(1),
        is_month_start       TINYINT(1),
        is_month_end         TINYINT(1),
        lag_7                FLOAT,
        lag_14               FLOAT,
        lag_30               FLOAT,
        lag_365              FLOAT,
        rolling_mean_7       FLOAT,
        rolling_mean_30      FLOAT,
        rolling_mean_90      FLOAT,
        rolling_std_7        FLOAT,
        trend_direction      FLOAT,
        yoy_growth           FLOAT,
        sales_next_7         FLOAT,
        features_computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_sale   (sale_date, item_id),
        INDEX idx_item_date  (item_id, sale_date),
        INDEX idx_date       (sale_date)
      )
    `);

    console.log("Database & tables ready!");
    await pool.end();

  } catch (error) {
    console.error("Error setting up database:", error.message);
    throw error;
  }
}

export async function createRack(pool, rack_code, type) {
  const [result] = await pool.query(
      "INSERT INTO racks (rack_code, type) VALUES (?, ?)",
      [rack_code, type]
  );
  return result.insertId;
}

export async function getRacks(pool) {
  const [rows] = await pool.query("SELECT * FROM racks");
  return rows;
}

export async function createBoxes(pool, shelf_id, count, max_units) {
  for (let i = 1; i <= count; i++) {
      const box_code = `BX-${shelf_id}-${i}`;

      await pool.query(
          "INSERT INTO boxes (box_code, shelf_id, max_units) VALUES (?, ?, ?)",
          [box_code, shelf_id, max_units]
      );
  }
}

export async function getBoxes(pool) {
  const [rows] = await pool.query(`
      SELECT b.*, s.shelf_code
      FROM boxes b
      LEFT JOIN shelves s ON b.shelf_id = s.id
  `);
  return rows;
}


export async function createShelves(pool, rack_id, levels) {
  for (let i = 1; i <= levels; i++) {
      const shelf_code = `SH-${rack_id}-L${i}`;

      await pool.query(
          "INSERT INTO shelves (shelf_code, rack_id, level) VALUES (?, ?, ?)",
          [shelf_code, rack_id, i]
      );
  }
}

export async function getShelves(pool) {
  const [rows] = await pool.query("SELECT * FROM shelves");
  return rows;
}


export async function createProduct(pool, data) {
  const {
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
  } = data;

  const [result] = await pool.query(
    `INSERT INTO products (
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
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
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
      created_by || null
    ]
  );

  return result.insertId;
}

export async function getProducts(pool) {
  const [rows] = await pool.query(`
      SELECT p.*, b.box_code
      FROM products p
      LEFT JOIN boxes b ON p.box_id = b.id
  `);
  return rows;
}

