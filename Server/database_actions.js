import fs from 'fs/promises' 
import { domainToUnicode } from 'url';
import bcrypt from 'bcrypt';
import jwt from 'jsonwebtoken';
import mysql from 'mysql2/promise'
import ExcelJS from 'exceljs/dist/es5/index.js';
// const ExcelJS = require('exceljs/dist/es5');


export async function setupDatabase() {
  try {
    const bootstrap = await mysql.createConnection({
      host:     process.env.DATABASE_HOST     || "localhost",
      user:     process.env.DATABASE_USER,
      password: process.env.DATABASE_PASSWORD,
      port:     +process.env.DATABASE_PORT    || 3306,
    });
    let databasename = process.env.DATABASE_NAME || "warehouse_optimizer";
    await bootstrap.query(`CREATE DATABASE IF NOT EXISTS ${databasename}`);
    await bootstrap.end();

    const pool = mysql.createPool({
      host:               process.env.DATABASE_HOST     || "localhost",
      user:               process.env.DATABASE_USER,
      password:           process.env.DATABASE_PASSWORD,
      port:               +process.env.DATABASE_PORT    || 3306,
      database:           databasename,
      waitForConnections: true,
      connectionLimit:    10,
    });

    const q = (sql, params=[]) => pool.query(sql, params);
    await q(`
      CREATE TABLE IF NOT EXISTS users(
          id INT AUTO_INCREMENT PRIMARY KEY,
          name VARCHAR(255),
          email VARCHAR(255) UNIQUE,
          role VARCHAR(50),
          password VARCHAR(255),
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
      `);

    const hashedPassword = await bcrypt.hash(
      process.env.DEMO_USER_PASSWORD || "demo123",
      10
    );

    await q(
      `INSERT IGNORE INTO users (name,email,password) VALUES (?,?,?)`,
      ['demo','demo@gmail.com',hashedPassword]
    );
    await q(`
      CREATE TABLE IF NOT EXISTS racks (
        id INT AUTO_INCREMENT PRIMARY KEY,
        rack_code VARCHAR(50) UNIQUE,
        type VARCHAR(50)
      );
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS shelves (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        shelf_code VARCHAR(50) UNIQUE,
        rack_id    INT,
        level      INT,
        FOREIGN KEY (rack_id)    REFERENCES racks(id)  ON DELETE CASCADE
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
        FOREIGN KEY (shelf_id)   REFERENCES shelves(id) ON DELETE CASCADE
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
        stock_qty         FLOAT DEFAULT 0,
        size_category     VARCHAR(50) DEFAULT 'medium',
        box_id            INT,
        reorder_level     INT,
        updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (box_id)     REFERENCES boxes(id) ON DELETE SET NULL
      )
    `);

    await q(`
      CREATE TABLE IF NOT EXISTS raw_materials (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        material_code     VARCHAR(50) UNIQUE,
        material_name     VARCHAR(255),
        category          VARCHAR(100),
        stock_qty         FLOAT DEFAULT 0,
        size_category     VARCHAR(50) DEFAULT 'medium',
        product_id        VARCHAR(50) DEFAULT NULL,
        qty_per_unit      FLOAT DEFAULT 0,
        box_id            INT,
        reorder_level     INT,
        updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products(product_code) ON DELETE SET NULL,
        FOREIGN KEY (box_id)     REFERENCES boxes(id)    ON DELETE SET NULL
      )
    `);
    await pool.query(`
      ALTER TABLE raw_materials
      ADD CONSTRAINT IF NOT EXISTS fk_rm_product
      FOREIGN KEY (product_id) REFERENCES products(product_code) ON DELETE SET NULL
    `).catch(() => {});


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
    stock_qty,
    size_category,
    box_id,
  } = data;

  const [result] = await pool.query(
    `INSERT INTO products (
      product_code,
      product_name,
      category,
      stock_qty,
      size_category,
      box_id,
    ) VALUES (?, ?, ?, ?, ?, ?)`,
    [
      product_code,
      product_name,
      category,
      stock_qty,
      size_category,
      box_id,
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

