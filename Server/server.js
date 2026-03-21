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

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app=express();
const port=process.env.PORT ||  8080
app.use(express.json())
app.use(cookieParser())
app.use(cors({
    origin: process.env.DOMAIN,
    credentials:true}))

app.get('/',(req,res)=>{
  res.send('Hello World')
})
app.get('/app',(req,res)=>{
  res.sendFile(path.join(__dirname, "../Client/app.html"))
})




app.listen(port,()=>{
  console.log(`Server started on port ${port}`)
  
})
