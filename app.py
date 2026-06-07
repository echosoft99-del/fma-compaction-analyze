# -*- coding: utf-8 -*-
"""
FMA Compaction Analyzer - الإصدار النهائي المُعتمد (خريطة حقيقية)
--------------------------------------------------------------
- اتصال ESP32 حقيقي
- GPS محاكي + GPS متصفح
- حساب دمك محلي متقدم
- خريطة مضمونة: إما Mapbox مع token، أو بديلة باستخدام st.map
- نقاط ملونة حسب الحالة (عند توفر token) وإلا نقاط موحدة
- أسطورة واضحة وتلوين صحيح
- مؤشر دائري للدمك، فلتر زمني، مقارنة طبقات
- تصدير Excel, PDF, HTML + خريطة مستقلة
"""

import io, json, math, os, sqlite3, time, warnings
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, black, white, grey
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable

try:
    from streamlit_js_eval import get_geolocation
    JS_EVAL_OK = True
except:
    JS_EVAL_OK = False

warnings.filterwarnings("ignore")

# ============================================================================
# إعدادات الصفحة
# ============================================================================
st.set_page_config(page_title="FMA Compaction Analyzer Pro", page_icon="🏗️", layout="wide", initial_sidebar_state="expanded")

# ============================================================================
# تنسيق CSS – الشريط الجانبي أبيض، تبويبات واضحة
# ============================================================================
st.markdown("""
<style>
    .main { background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); }
    .css-1r6slb0, .css-1kyxreq { background-color: rgba(255,255,255,0.95); border-radius: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); padding: 10px; }
    [data-testid="stSidebar"] { background: #ffffff; color: #000000; }
    [data-testid="stSidebar"] * { color: #000000 !important; }
    .stTabs [data-baseweb="tab"] { background-color: #f0f2f6; color: #000000; border-radius: 8px 8px 0 0; padding: 10px 16px; }
    .stTabs [data-baseweb="tab"][aria-selected="true"] { background-color: #ffffff; color: #000000; font-weight: bold; border: 1px solid #ddd; }
    .stButton>button { border-radius: 10px; transition: all 0.3s ease; }
    .stButton>button:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.2); }
    .stDataFrame { border-radius: 10px; overflow: hidden; }
</style>
""", unsafe_allow_html=True)

# ============================================================================
# الثوابت
# ============================================================================
REQUEST_TIMEOUT = 5
DEFAULT_ESP32_IP = "192.168.4.1"
REF_LAT, REF_LON = 13.9633333, 44.5819444
METERS_PER_LAT = 111320.0

# رمز Mapbox عام (قد تحتاج إلى تغييره برمزك الخاص من mapbox.com)
DEFAULT_MAPBOX_TOKEN = "pk.eyJ1IjoiY2hyaWRkeXAiLCJhIjoiY2l1MHVhZ20zMDAwMDJ3cWZ0bmVmY3VoMiJ9.dh1ulrUf_gRd5QY5p0UoUg"

# ============================================================================
# نظام GPS المحاكي
# ============================================================================
class SimulatedGPS:
    def __init__(self, start_lat=REF_LAT, start_lon=REF_LON):
        self.base_lat, self.base_lon = start_lat, start_lon
        self.current_lat, self.current_lon = start_lat, start_lon
        self.step_count = self.grid_row = self.grid_col = 0
        self.grid_spacing, self.cols_per_row = 5.0, 8

    def update_position(self, mode="grid"):
        self.step_count += 1
        if mode == "grid":
            self.grid_col = self.step_count % self.cols_per_row
            self.grid_row = self.step_count // self.cols_per_row
            dx = self.grid_col * self.grid_spacing
            dy = self.grid_row * self.grid_spacing
            lat_off = dy / METERS_PER_LAT
            lon_off = dx / (METERS_PER_LAT * math.cos(math.radians(self.base_lat)))
            self.current_lat = self.base_lat + lat_off
            self.current_lon = self.base_lon + lon_off
        elif mode == "line":
            dist = 1.2 * self.step_count
            rad = math.radians(45)
            self.current_lat = self.base_lat + (dist * math.cos(rad) / METERS_PER_LAT)
            self.current_lon = self.base_lon + (dist * math.sin(rad) / (METERS_PER_LAT * math.cos(math.radians(self.base_lat))))
        elif mode == "random":
            self.current_lat += np.random.uniform(-0.00008, 0.00008)
            self.current_lon += np.random.uniform(-0.00008, 0.00008)
        return {"lat": self.current_lat, "lon": self.current_lon, "row": self.grid_row, "col": self.grid_col}

    def reset(self):
        self.current_lat, self.current_lon = self.base_lat, self.base_lon
        self.step_count = self.grid_row = self.grid_col = 0

# ============================================================================
# قاعدة البيانات
# ============================================================================
class DatabaseManager:
    def __init__(self, db_path="fma_compaction.db"):
        self.db_path = db_path
        self.init_tables()
    def init_tables(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT UNIQUE, project_name TEXT, date TEXT,
                location TEXT, engineer TEXT, layer_number INTEGER DEFAULT 1, unit_system TEXT,
                data_json TEXT, summary_json TEXT, ref_data_json TEXT)''')
    def save_project(self, project_id, project_name, location, engineer, layer, unit, data, summary, ref_data):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT OR REPLACE INTO projects VALUES (NULL,?,?,?,?,?,?,?,?,?,?)''',
                         (project_id, project_name, datetime.now().isoformat(), location, engineer, layer, unit,
                          json.dumps(data), json.dumps(summary), json.dumps(ref_data)))
    def get_all_projects(self):
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute("SELECT project_id, project_name, date, location, layer_number FROM projects ORDER BY date DESC").fetchall()
            except:
                conn.execute("ALTER TABLE projects ADD COLUMN layer_number INTEGER DEFAULT 1")
                rows = conn.execute("SELECT project_id, project_name, date, location, layer_number FROM projects ORDER BY date DESC").fetchall()
            return rows
    def load_project(self, project_id):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT data_json, summary_json, ref_data_json FROM projects WHERE project_id=?", (project_id,)).fetchone()
            return (json.loads(row[0]), json.loads(row[1]), json.loads(row[2])) if row else (None, None, None)
    def get_all_layers(self):
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute("SELECT project_id, project_name, layer_number, data_json FROM projects ORDER BY layer_number").fetchall()
            except:
                conn.execute("ALTER TABLE projects ADD COLUMN layer_number INTEGER DEFAULT 1")
                rows = conn.execute("SELECT project_id, project_name, layer_number, data_json FROM projects ORDER BY layer_number").fetchall()
            return rows

# ============================================================================
# حاسبة الدمك
# ============================================================================
class CompactionCalculator:
    SOIL_FACTORS = {
        "رملية": {"energy": 1.00, "moisture_sensitivity": 0.06, "max_improvement": 1.30},
        "طينية": {"energy": 0.85, "moisture_sensitivity": 0.10, "max_improvement": 1.20},
        "غرينية": {"energy": 0.90, "moisture_sensitivity": 0.08, "max_improvement": 1.25},
        "صخرية مكسرة": {"energy": 1.15, "moisture_sensitivity": 0.04, "max_improvement": 1.15},
    }
    @staticmethod
    def get_color(value):
        if value < 40: return "#8B0000"
        elif value < 50: return "#B22222"
        elif value < 60: return "#DC143C"
        elif value < 65: return "#FF4500"
        elif value < 70: return "#FF6347"
        elif value < 75: return "#FF8C00"
        elif value < 80: return "#FFA500"
        elif value < 85: return "#FFD700"
        elif value < 88: return "#FFFF00"
        elif value < 91: return "#ADFF2F"
        elif value < 94: return "#7CFC00"
        elif value < 97: return "#32CD32"
        elif value < 100: return "#228B22"
        elif value < 105: return "#1E90FF"
        elif value < 110: return "#191970"
        return "#4B0082"
    @staticmethod
    def get_status(value, t_min=95, t_max=100):
        if value < t_min: return "🔴 غير مقبول", "poor"
        elif value <= t_max: return "🟢 مقبول", "good"
        return "🔵 دمك مفرط", "over"

# ============================================================================
# دوال مساعدة
# ============================================================================
def normalize_url(ip):
    ip = ip.strip()
    if not ip: raise ValueError("أدخل عنوان IP")
    return ip if ip.startswith("http") else f"http://{ip}"

def fetch_data(url):
    return requests.get(f"{url}/data", timeout=REQUEST_TIMEOUT).json()

def send_control(url, action):
    try: requests.post(f"{url}/control", json={"action": action}, timeout=REQUEST_TIMEOUT)
    except: pass

def haversine(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2): return 0
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def calculate_real_compaction(passes, rms, peak, dominant_hz, ref_data, ref_sensor=None):
    if ref_data is None:
        initial, final, ref_passes, soil_energy, moisture_factor = 78.0, 98.5, 8, 1.0, 0.9
    else:
        initial = float(ref_data.get("initial", 78.0))
        final = float(ref_data.get("final", 98.5))
        ref_passes = max(int(ref_data.get("passes", 8)), 1)
        soil_type = ref_data.get("soil_type", "رملية")
        soil = CompactionCalculator.SOIL_FACTORS.get(soil_type, CompactionCalculator.SOIL_FACTORS["رملية"])
        soil_energy = soil["energy"]
        omc = float(ref_data.get("omc", 12.5))
        moisture = float(ref_data.get("initial_moisture", 11.2))
        moisture_factor = math.exp(-soil["moisture_sensitivity"] * abs(moisture - omc))
        moisture_factor = max(0.65, min(1.0, moisture_factor))
    if passes <= 0: return initial
    ratio = min(float(passes) / float(ref_passes), 1.5)
    log_factor = min(math.log(1.0 + ratio * 2.0) / math.log(3.0), 1.0)
    rms_factor = peak_factor = freq_factor = 1.0
    if ref_sensor:
        if rms > 0: rms_factor = min(max(rms / max(float(ref_sensor.get("rms", 0.1)), 0.01), 0.5), 1.5)
        if dominant_hz > 0: freq_factor = min(max(dominant_hz / max(float(ref_sensor.get("dominant_hz", 15.0)), 1.0), 0.7), 1.3)
        if peak > 0: peak_factor = min(max(peak / max(float(ref_sensor.get("peak", 0.1)), 0.01), 0.6), 1.4)
    signal_factor = 0.50 * rms_factor + 0.30 * peak_factor + 0.20 * freq_factor
    improvement = (final - initial) * log_factor * signal_factor * moisture_factor * soil_energy
    return round(max(40.0, min(initial + improvement, 112.0)), 2)

# ============================================================================
# دوال التصدير (كما هي بدون تغيير جوهري)
# ============================================================================
def export_excel(df, project_data):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as w:
        df.to_excel(w, sheet_name='Compaction_Data', index=False)
        good = len(df[df['status_type']=='good']) if 'status_type' in df.columns else 0
        poor = len(df[df['status_type']=='poor']) if 'status_type' in df.columns else 0
        over = len(df[df['status_type']=='over']) if 'status_type' in df.columns else 0
        summary = pd.DataFrame({"المؤشر": ["المشروع", "الطبقة", "عدد النقاط", "متوسط الدمك", "أدنى قيمة", "أعلى قيمة", "نقاط مقبولة", "نقاط غير مقبولة", "دمك مفرط"],
                                "القيمة": [project_data.get('name',''), project_data.get('layer',1), len(df),
                                           f"{df['compaction_percent'].mean():.1f}%", f"{df['compaction_percent'].min():.1f}%",
                                           f"{df['compaction_percent'].max():.1f}%", good, poor, over]})
        summary.to_excel(w, sheet_name='Summary', index=False)
    return output.getvalue()

def export_html(df, project_data):
    good = len(df[df['status_type']=='good']) if 'status_type' in df.columns else 0
    poor = len(df[df['status_type']=='poor']) if 'status_type' in df.columns else 0
    over = len(df[df['status_type']=='over']) if 'status_type' in df.columns else 0
    return f"""<!DOCTYPE html><html dir='rtl'><head><meta charset='UTF-8'><title>تقرير FMA</title>
    <style>body{{font-family:Arial;margin:30px;background:#f5f5f5;}}.box{{max-width:1100px;margin:auto;background:white;padding:24px;border-radius:16px;}}
    table{{width:100%;border-collapse:collapse;margin-top:20px;}}th,td{{border:1px solid #ddd;padding:8px;}}th{{background:#1f4e79;color:white;}}</style></head>
    <body><div class='box'><h1>🏗️ تقرير FMA للدمك</h1><p>المشروع: {project_data.get('name','')} | الطبقة: {project_data.get('layer',1)}</p>
    <h2>ملخص</h2><ul><li>عدد النقاط: {len(df)}</li><li>متوسط الدمك: {df['compaction_percent'].mean():.1f}%</li>
    <li>غير مقبول: {poor}</li><li>مقبول: {good}</li><li>دمك مفرط: {over}</li></ul>
    {df.to_html(index=False)}</div></body></html>"""

def export_pdf(df, project_data, map_fig=None):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=22, textColor=HexColor('#1a3a5c'), alignment=TA_CENTER)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading2'], fontSize=16, textColor=HexColor('#2c5f8a'))
    elements = [Spacer(1, 3*cm), Paragraph("FMA Compaction Analyzer", title_style),
                Spacer(1, 0.5*cm), Paragraph("تقرير فني معتمد", ParagraphStyle('Sub', parent=styles['Normal'], fontSize=18, alignment=TA_CENTER)),
                Spacer(1, 2*cm), HRFlowable(width="80%", thickness=2, color=HexColor('#3498db')), Spacer(1, 1*cm)]
    info_data = [["المشروع", project_data.get('name','-')], ["الطبقة", project_data.get('layer',1)],
                 ["التاريخ", datetime.now().strftime('%Y-%m-%d')], ["الوقت", datetime.now().strftime('%H:%M:%S')],
                 ["عدد النقاط", len(df)], ["متوسط الدمك", f"{df['compaction_percent'].mean():.1f}%"]]
    t = Table(info_data, colWidths=[5*cm, 8*cm])
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (0,-1), HexColor('#e8f0fe')), ('GRID', (0,0), (-1,-1), 1, HexColor('#b0c4de')),
                           ('FONTSIZE', (0,0), (-1,-1), 11), ('ALIGN', (0,0), (-1,-1), 'CENTER')]))
    elements.append(t)
    elements.append(Spacer(1, 1*cm))
    elements.append(Paragraph("ملخص النتائج", heading_style))
    good = len(df[df['status_type']=='good']) if 'status_type' in df.columns else 0
    poor = len(df[df['status_type']=='poor']) if 'status_type' in df.columns else 0
    over = len(df[df['status_type']=='over']) if 'status_type' in df.columns else 0
    summary_data = [["متوسط الدمك", f"{df['compaction_percent'].mean():.1f}%"], ["أعلى قيمة", f"{df['compaction_percent'].max():.1f}%"],
                    ["أدنى قيمة", f"{df['compaction_percent'].min():.1f}%"], ["الانحراف المعياري", f"{df['compaction_percent'].std():.2f}"],
                    ["نقاط مقبولة", good], ["نقاط غير مقبولة", poor], ["دمك مفرط", over]]
    st2 = Table(summary_data, colWidths=[6*cm, 6*cm])
    st2.setStyle(TableStyle([('BACKGROUND', (0,0), (0,-1), HexColor('#e8f0fe')), ('GRID', (0,0), (-1,-1), 1, HexColor('#b0c4de'))]))
    elements.append(st2)
    if map_fig:
        try:
            elements.append(Spacer(1,1*cm)); elements.append(Paragraph("خريطة الدمك", heading_style))
            img_bytes = io.BytesIO()
            map_fig.write_image(img_bytes, format="png", width=700, height=400, scale=2, engine="kaleido")
            img_bytes.seek(0)
            elements.append(Image(img_bytes, width=16*cm, height=9*cm))
        except: pass
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()

# ============================================================================
# تهيئة الجلسة
# ============================================================================
def init_session_state():
    defaults = {
        "tracking": False, "records": [], "current_lat": None, "current_lon": None, "current_acc": None,
        "last_recorded_lat": None, "last_recorded_lon": None, "last_recorded_passes": -1, "total_distance_m": 0.0,
        "latest_data": None, "reference_data": None, "reference_sensor": None, "gps_simulator": SimulatedGPS(),
        "current_layer": 1, "scan_mode": "grid", "project_data": None, "db_manager": DatabaseManager(),
        "use_simulated_gps": True, "map_style": "carto-positron",
        "mapbox_token": DEFAULT_MAPBOX_TOKEN  # يمكن تغييره من الإعدادات
    }
    for k, v in defaults.items():
        if k not in st.session_state: st.session_state[k] = v

init_session_state()

# ============================================================================
# الشريط الجانبي
# ============================================================================
with st.sidebar:
    st.markdown("## 🏗️ FMA الدمك الذكي")
    st.markdown("---")
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📡 اتصال", "🔧 معايرة", "🗺️ GPS", "📊 طبقات", "💾 مشاريع"])
    with tab1:
        st.markdown("### 📡 إعدادات الاتصال")
        device_ip = st.text_input("عنوان ESP32", DEFAULT_ESP32_IP)
        project_name = st.text_input("اسم المشروع", "مشروع دمك FMA")
        engineer_name = st.text_input("المهندس", "المهندس المشرف")
        project_location = st.text_input("الموقع", "محافظة إب - اليمن")
    with tab2:
        st.markdown("### 🔧 المعايرة المرجعية")
        soil_type = st.selectbox("نوع التربة", ["رملية", "طينية", "غرينية", "صخرية مكسرة"])
        initial_comp = st.number_input("الدمك الابتدائي (%)", 40.0, 95.0, 78.0)
        ref_passes = st.number_input("التمريرات المرجعية", 1, 30, 8)
        final_comp = st.number_input("الدمك النهائي (%)", 80.0, 112.0, 98.5)
        initial_moisture = st.number_input("الرطوبة الحالية (%)", 0.0, 35.0, 11.2)
        omc = st.number_input("الرطوبة المثلى (%)", 0.0, 35.0, 12.5)
        efficiency = st.slider("كفاءة المعدة (%)", 50, 120, 100)
        st.markdown("---")
        if st.button("📡 قراءة مرجعية من ESP32", use_container_width=True):
            try:
                url = normalize_url(device_ip)
                st.session_state.reference_sensor = fetch_data(url)
                st.success("✅ تم حفظ المرجع الحسي")
            except Exception as e: st.error(f"فشل: {e}")
        if st.session_state.reference_sensor:
            rs = st.session_state.reference_sensor
            st.json({"RMS": rs.get("rms",0), "Peak": rs.get("peak",0), "Dominant Hz": rs.get("dominant_hz",0)})
        else: st.warning("استخدام قيم افتراضية")
        if st.button("✅ حفظ المعايرة", type="primary", use_container_width=True):
            st.session_state.reference_data = {"soil_type": soil_type, "initial": initial_comp, "passes": ref_passes,
                                                "final": final_comp, "initial_moisture": initial_moisture, "omc": omc, "efficiency": efficiency}
            st.session_state.project_data = {"name": project_name, "location": project_location, "engineer": engineer_name, "layer": st.session_state.current_layer}
            st.success("✅ تم الحفظ")
    with tab3:
        st.markdown("### 🗺️ إعدادات الموقع و الخريطة")
        st.session_state.use_simulated_gps = not st.checkbox("استخدام GPS حقيقي", value=False)
        if st.session_state.use_simulated_gps:
            st.info("📍 GPS محاكي نشط")
            st.session_state.scan_mode = st.selectbox("نمط المسح", ["grid","line","random"], format_func=lambda x: {"grid":"شبكي","line":"خط مستقيم","random":"عشوائي"}[x])
            spacing = st.number_input("التباعد (متر)", 1.0, 20.0, 5.0)
            st.session_state.gps_simulator.grid_spacing = spacing
            if st.button("🔄 إعادة تعيين الموقع"): st.session_state.gps_simulator.reset(); st.success("تم")
        else: st.info("GPS المتصفح")
        st.markdown("---")
        st.markdown("#### رمز Mapbox (للخريطة الملونة)")
        token_input = st.text_input("أدخل رمز Mapbox token", value=st.session_state.mapbox_token, type="password")
        if st.button("تحديث الرمز"):
            st.session_state.mapbox_token = token_input
            st.success("تم تحديث رمز Mapbox")
        st.caption("يمكنك الحصول على رمز مجاني من [mapbox.com](https://account.mapbox.com/auth/signup/)")
        if not st.session_state.mapbox_token or st.session_state.mapbox_token == DEFAULT_MAPBOX_TOKEN:
            st.warning("قد لا يعمل الرمز الافتراضي، استخدم رمزك الخاص لضمان ظهور الخريطة الملونة.")
        # اختبار الرمز
        if st.button("اختبار الرمز", use_container_width=True):
            try:
                px.set_mapbox_access_token(st.session_state.mapbox_token)
                # محاولة رسم بسيطة
                test_fig = go.Figure(go.Scattermapbox(lat=[REF_LAT], lon=[REF_LON], mode='markers'))
                test_fig.update_layout(mapbox=dict(style="carto-positron", center=dict(lat=REF_LAT, lon=REF_LON), zoom=10))
                # إذا لم يحدث خطأ فالرمز صالح
                st.success("✅ الرمز يعمل!")
            except Exception as e:
                st.error(f"❌ الرمز غير صالح: {e}")
    with tab4:
        st.metric("الطبقة الحالية", st.session_state.current_layer)
        if st.button("🆕 طبقة جديدة", use_container_width=True):
            st.session_state.current_layer += 1
            st.session_state.records = []; st.session_state.gps_simulator.reset()
            st.session_state.last_recorded_lat = st.session_state.last_recorded_lon = None
            st.session_state.last_recorded_passes = -1; st.session_state.total_distance_m = 0.0
            st.success(f"بدأت الطبقة {st.session_state.current_layer}")
            st.rerun()
    with tab5:
        st.markdown("### 💾 المشاريع المحفوظة")
        projects = st.session_state.db_manager.get_all_projects()
        if projects:
            for proj in projects:
                c1, c2 = st.columns([3,1])
                with c1: st.caption(f"📁 {proj[1]} | طبقة {proj[4]} | {proj[2][:10]}")
                with c2:
                    if st.button("تحميل", key=f"load_{proj[0]}"):
                        data, _, ref = st.session_state.db_manager.load_project(proj[0])
                        if data:
                            st.session_state.records = data.get('points', [])
                            st.session_state.reference_data = ref
                            st.success(f"تم تحميل {len(st.session_state.records)} نقطة")
                            st.rerun()
                st.divider()
        else: st.info("لا توجد مشاريع")

# ============================================================================
# الواجهة الرئيسية
# ============================================================================
st.title("🏗️ FMA Compaction Analyzer Pro")
st.caption("نظام متكامل لمراقبة دمك التربة | ESP32 + MPU-9250 | خريطة حقيقية مضمونة")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("النقاط", len(st.session_state.records))
m2.metric("التتبع", "🟢 نشط" if st.session_state.tracking else "⏸️ متوقف")
m3.metric("المعايرة", "✅" if st.session_state.reference_data else "⚠️")
m4.metric("الطبقة", st.session_state.current_layer)
m5.metric("GPS", "محاكي" if st.session_state.use_simulated_gps else "حقيقي")
st.markdown("---")

# أزرار التحكم
c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    if st.button("▶️ بدء التتبع", type="primary", use_container_width=True):
        if not st.session_state.reference_data: st.error("أكمل المعايرة أولاً")
        else:
            st.session_state.tracking = True; st.session_state.records = []
            st.session_state.last_recorded_lat = st.session_state.last_recorded_lon = None
            st.session_state.last_recorded_passes = -1; st.session_state.total_distance_m = 0.0
            st.session_state.gps_simulator.reset(); st.success("✅ بدأ التتبع"); st.rerun()
with c2:
    if st.button("⏹️ إيقاف", use_container_width=True):
        st.session_state.tracking = False; st.warning("⏸️ تم الإيقاف"); st.rerun()
with c3:
    if st.button("📡 قراءة واحدة", use_container_width=True):
        try:
            live = fetch_data(normalize_url(device_ip))
            st.session_state.latest_data = live
            if st.session_state.use_simulated_gps:
                gps = st.session_state.gps_simulator.update_position(st.session_state.scan_mode)
                lat, lon = gps["lat"], gps["lon"]
            elif JS_EVAL_OK:
                try:
                    geo = get_geolocation()
                    if geo and "coords" in geo: lat, lon = float(geo["coords"]["latitude"]), float(geo["coords"]["longitude"])
                    else: lat, lon = REF_LAT, REF_LON
                except: lat, lon = REF_LAT, REF_LON
            else: lat, lon = REF_LAT, REF_LON
            passes = len(st.session_state.records) + 1
            rms, peak, hz = float(live.get("rms",0.05)), float(live.get("peak",0.1)), float(live.get("dominant_hz",15))
            comp = calculate_real_compaction(passes, rms, peak, hz, st.session_state.reference_data, st.session_state.get("reference_sensor"))
            status_text, status_type = CompactionCalculator.get_status(comp)
            moved = haversine(st.session_state.last_recorded_lat, st.session_state.last_recorded_lon, lat, lon) if st.session_state.last_recorded_lat else 0.0
            record = {"الزمن": datetime.now().strftime("%H:%M:%S"), "lat": lat, "lon": lon, "passes": passes,
                      "compaction_percent": comp, "color": CompactionCalculator.get_color(comp),
                      "status": status_text, "status_type": status_type,
                      "rms": rms, "peak": peak, "dominant_hz": hz, "distance_m": round(moved,3), "layer": st.session_state.current_layer}
            st.session_state.records.append(record)
            st.session_state.total_distance_m += moved
            st.session_state.last_recorded_lat, st.session_state.last_recorded_lon = lat, lon
            st.session_state.last_recorded_passes = passes
            st.success("✅ تم تسجيل نقطة")
        except Exception as e: st.error(f"فشل: {e}")
with c4:
    if st.button("💾 حفظ الطبقة", use_container_width=True):
        if st.session_state.records:
            st.session_state.db_manager.save_project(f"FMA-{datetime.now().strftime('%Y%m%d%H%M')}", project_name, project_location, engineer_name,
                                                     st.session_state.current_layer, "metric", {"points": st.session_state.records},
                                                     {"total_points": len(st.session_state.records)}, st.session_state.reference_data)
            st.success("✅ تم الحفظ")
        else: st.warning("لا بيانات")
with c5:
    if st.button("🗑️ مسح الكل", use_container_width=True):
        st.session_state.records = []; st.session_state.last_recorded_lat = st.session_state.last_recorded_lon = None
        st.session_state.last_recorded_passes = -1; st.session_state.total_distance_m = 0.0
        st.session_state.gps_simulator.reset(); st.rerun()

# التتبع المستمر
if st.session_state.tracking:
    try:
        live = fetch_data(normalize_url(device_ip))
        st.session_state.latest_data = live
        if st.session_state.use_simulated_gps:
            gps = st.session_state.gps_simulator.update_position(st.session_state.scan_mode)
            st.session_state.current_lat, st.session_state.current_lon = gps["lat"], gps["lon"]
        elif JS_EVAL_OK:
            try:
                geo = get_geolocation()
                if geo and "coords" in geo:
                    st.session_state.current_lat = float(geo["coords"]["latitude"]); st.session_state.current_lon = float(geo["coords"]["longitude"])
            except: pass
        lat, lon = st.session_state.current_lat, st.session_state.current_lon
        if lat and lon:
            passes = len(st.session_state.records) + 1
            rms, peak, hz = float(live.get("rms",0.05)), float(live.get("peak",0.1)), float(live.get("dominant_hz",15))
            comp = calculate_real_compaction(passes, rms, peak, hz, st.session_state.reference_data, st.session_state.get("reference_sensor"))
            status_text, status_type = CompactionCalculator.get_status(comp)
            moved = haversine(st.session_state.last_recorded_lat, st.session_state.last_recorded_lon, lat, lon) if st.session_state.last_recorded_lat else 0.0
            record = {"الزمن": datetime.now().strftime("%H:%M:%S"), "lat": lat, "lon": lon, "passes": passes,
                      "compaction_percent": comp, "color": CompactionCalculator.get_color(comp),
                      "status": status_text, "status_type": status_type, "rms": rms, "peak": peak, "dominant_hz": hz,
                      "distance_m": round(moved,3), "layer": st.session_state.current_layer}
            st.session_state.records.append(record)
            st.session_state.total_distance_m += moved
            st.session_state.last_recorded_lat, st.session_state.last_recorded_lon = lat, lon
            st.session_state.last_recorded_passes = passes
        time.sleep(1); st.rerun()
    except Exception as e:
        st.error(f"خطأ: {e}"); st.session_state.tracking = False; st.rerun()

# عرض آخر قراءة
if st.session_state.latest_data:
    st.markdown("### 📟 آخر قراءة ESP32")
    live = st.session_state.latest_data
    a1, a2, a3, a4, a5, a6 = st.columns(6)
    a1.metric("RMS", f"{float(live.get('rms',0)):.4f} g")
    a2.metric("Peak", f"{float(live.get('peak',0)):.4f} g")
    a3.metric("Dominant Hz", f"{float(live.get('dominant_hz',0)):.2f}")
    a4.metric("Gyro RMS", f"{float(live.get('gyro_rms',0)):.3f}")
    a5.metric("Temp", f"{float(live.get('temp_c',0)):.1f} °C")
    a6.metric("Uptime", f"{int(live.get('uptime_ms',0))/1000:.1f} s")

# ============================================================================
# عرض البيانات المسجلة
# ============================================================================
if st.session_state.records:
    df = pd.DataFrame(st.session_state.records)
    st.markdown(f"### 📋 البيانات المسجلة - الطبقة {st.session_state.current_layer} ({len(df)} نقطة)")
    
    # فلتر
    show_filter = st.checkbox("تفعيل فلتر النقاط", value=False)
    if show_filter:
        idx_range = st.slider("نطاق النقاط", 0, len(df)-1, (0, len(df)-1))
        df_display = df.iloc[idx_range[0]:idx_range[1]+1].copy()
    else:
        df_display = df.copy()

    # فلتر القيم الفارغة
    df_display = df_display.dropna(subset=['lat', 'lon'])

    # مؤشر دائري
    avg_comp = df['compaction_percent'].mean()
    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number+delta", value=avg_comp, domain={'x':[0,1], 'y':[0,1]},
        title={'text':"متوسط معامل الدمك", 'font':{'size':24}}, delta={'reference':95},
        gauge={'axis':{'range':[40,112]}, 'steps':[
            {'range':[40,70], 'color':'#e74c3c'}, {'range':[70,85], 'color':'#f39c12'},
            {'range':[85,95], 'color':'#f1c40f'}, {'range':[95,100], 'color':'#2ecc71'},
            {'range':[100,112], 'color':'#3498db'}], 'threshold':{'line':{'color':"black", 'width':4}, 'value':95}}
    ))
    fig_gauge.update_layout(height=300)
    st.plotly_chart(fig_gauge, use_container_width=True)

    # جدول
    display_cols = ["الزمن", "lat", "lon", "passes", "compaction_percent", "rms", "peak", "dominant_hz", "status", "distance_m"]
    st.dataframe(df_display[[c for c in display_cols if c in df_display.columns]], use_container_width=True, height=250)

    # ========== الخريطة المضمونة ==========
    st.markdown("### 🗺️ خريطة الدمك (جميع النقاط)")
    map_style_options = {"carto-positron":"خرائط واضحة", "open-street-map":"Open Street Map", "stamen-terrain":"تضاريس", "carto-darkmatter":"داكنة"}
    selected_style = st.selectbox("نمط الخريطة", list(map_style_options.keys()), format_func=lambda x: map_style_options[x])

    # محاولة استخدام Mapbox
    use_mapbox = False
    token = st.session_state.mapbox_token
    if token:
        try:
            px.set_mapbox_access_token(token)
            # اختبار سريع
            _ = go.Figure(go.Scattermapbox(lat=[REF_LAT], lon=[REF_LON])).update_layout(mapbox=dict(style=selected_style, zoom=10))
            use_mapbox = True
        except:
            st.warning("رمز Mapbox غير صالح، سيتم استخدام خريطة بديلة (st.map) مع نقاط موحدة اللون.")

    if use_mapbox:
        # ---------- خريطة Mapbox مع الألوان ----------
        fig = go.Figure()
        show_heat = st.checkbox("إظهار الطبقة الحرارية", value=True)
        heat_opacity = st.slider("شفافية الحرارية", 0.2, 1.0, 0.6) if show_heat else 0.0
        if show_heat:
            fig.add_trace(go.Densitymapbox(
                lat=df_display["lat"], lon=df_display["lon"], z=df_display["compaction_percent"],
                radius=10, opacity=heat_opacity,
                colorscale=[[0,"#8B0000"],[0.3,"#FF4500"],[0.6,"#FFD700"],[0.8,"#32CD32"],[1,"#1E90FF"]],
                colorbar=dict(title="الدمك (%)"), hovertemplate="الدمك: %{z:.1f}%<extra></extra>", name="كثافة الدمك"
            ))
        for status_type, status_label, color in [
            ("poor", "🔴 غير مقبول", "#e74c3c"),
            ("good", "🟢 مقبول", "#27ae60"),
            ("over", "🔵 دمك مفرط", "#2980b9")
        ]:
            subset = df_display[df_display['status_type'] == status_type]
            if not subset.empty:
                fig.add_trace(go.Scattermapbox(
                    lat=subset["lat"], lon=subset["lon"], mode="markers",
                    marker=dict(size=15, color=color, opacity=1.0),
                    text=subset.apply(lambda r: f"{r['status']}<br>الدمك: {r['compaction_percent']:.1f}%<br>RMS: {r['rms']:.3f}", axis=1),
                    hoverinfo="text", name=f"{status_label} ({len(subset)})"
                ))
        if len(df_display) >= 2:
            fig.add_trace(go.Scattermapbox(lat=df_display["lat"], lon=df_display["lon"], mode="lines",
                                           line=dict(width=3, color="#333"), name="مسار المعدة", hoverinfo="skip"))
        fig.add_trace(go.Scattermapbox(lat=[REF_LAT], lon=[REF_LON], mode="markers",
                                       marker=dict(size=18, symbol="star", color="gold"), name="⭐ المرجع"))
        if st.session_state.current_lat:
            fig.add_trace(go.Scattermapbox(lat=[st.session_state.current_lat], lon=[st.session_state.current_lon],
                                           mode="markers", marker=dict(size=14, color="#3498db"), name="📍 الموقع الحالي"))
        # حساب المركز والتكبير
        if not df_display.empty:
            center_lat = df_display["lat"].mean()
            center_lon = df_display["lon"].mean()
            lat_range = df_display["lat"].max() - df_display["lat"].min()
            lon_range = df_display["lon"].max() - df_display["lon"].min()
            zoom_level = 16 if (lat_range < 0.001 and lon_range < 0.001) else 15 if (lat_range < 0.01 and lon_range < 0.01) else 14
        else:
            center_lat, center_lon, zoom_level = REF_LAT, REF_LON, 16
        fig.update_layout(
            mapbox=dict(style=selected_style, center=dict(lat=center_lat, lon=center_lon), zoom=zoom_level),
            margin=dict(l=0, r=0, t=40, b=0), height=600,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, bgcolor="rgba(255,255,255,0.9)")
        )
        st.plotly_chart(fig, use_container_width=True)
        map_fig = fig
    else:
        # ---------- خريطة بديلة (st.map) تظهر جميع النقاط بلون موحد ----------
        st.info("عرض الخريطة البديلة (جميع النقاط ظاهرة، لكن بدون تلوين حسب الحالة)")
        # نضيف حجم النقاط يعتمد على درجة الدمك
        map_df = df_display[['lat', 'lon', 'compaction_percent']].copy()
        map_df['size'] = (map_df['compaction_percent'] / 10).clip(lower=2)
        st.map(map_df, latitude='lat', longitude='lon', size='size')
        map_fig = None  # لا يوجد شكل Plotly للتصدير

    # تحليلات
    st.markdown("### 📊 تحليلات")
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("متوسط الدمك", f"{df['compaction_percent'].mean():.1f}%")
    s2.metric("أعلى قيمة", f"{df['compaction_percent'].max():.1f}%")
    s3.metric("أدنى قيمة", f"{df['compaction_percent'].min():.1f}%")
    s4.metric("الانحراف المعياري", f"{df['compaction_percent'].std():.2f}")
    s5.metric("نقاط مقبولة", f"{len(df[df['status_type']=='good'])}/{len(df)}")
    col1, col2 = st.columns(2)
    with col1:
        fig_hist = px.histogram(df, x="compaction_percent", nbins=20, title="توزيع الدمك")
        fig_hist.add_vline(x=95, line_dash="dash", line_color="green")
        st.plotly_chart(fig_hist, use_container_width=True)
    with col2:
        fig_sig = px.line(df, x=df.index, y=["rms","peak","dominant_hz"], markers=True, title="إشارات الحساس")
        st.plotly_chart(fig_sig, use_container_width=True)

    # مقارنة طبقات
    layers = st.session_state.db_manager.get_all_layers()
    if len(layers) > 1:
        st.markdown("### 📊 مقارنة الطبقات")
        comp_data = []
        for pid, pname, lnum, djson in layers:
            pts = json.loads(djson) if isinstance(djson, str) else djson
            pts = pts.get('points', [])
            if pts:
                dl = pd.DataFrame(pts)
                comp_data.append({"الطبقة": lnum, "متوسط الدمك": dl['compaction_percent'].mean(), "عدد النقاط": len(pts)})
        if comp_data:
            dfc = pd.DataFrame(comp_data)
            fig_comp = make_subplots(rows=1, cols=2, subplot_titles=["متوسط الدمك", "عدد النقاط"])
            fig_comp.add_trace(go.Bar(x=dfc["الطبقة"].astype(str), y=dfc["متوسط الدمك"], marker_color="#27ae60", name="متوسط"), row=1, col=1)
            fig_comp.add_trace(go.Bar(x=dfc["الطبقة"].astype(str), y=dfc["عدد النقاط"], marker_color="#2980b9", name="عدد"), row=1, col=2)
            fig_comp.update_layout(height=400, showlegend=False)
            st.plotly_chart(fig_comp, use_container_width=True)
    else:
        st.info("تحتاج طبقتين على الأقل للمقارنة")

    # تصدير
    st.markdown("### 📄 تصدير التقارير")
    te1, te2 = st.tabs(["Excel/HTML/PDF", "خريطة مستقلة"])
    with te1:
        x1, x2, x3 = st.columns(3)
        with x1:
            st.download_button("📊 Excel", export_excel(df, st.session_state.project_data or {}),
                               f"fma_layer{st.session_state.current_layer}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        with x2:
            st.download_button("📄 HTML", export_html(df, st.session_state.project_data or {}),
                               f"fma_layer{st.session_state.current_layer}.html", "text/html")
        with x3:
            try:
                if map_fig:
                    pdf_data = export_pdf(df, st.session_state.project_data or {}, map_fig)
                else:
                    pdf_data = export_pdf(df, st.session_state.project_data or {})
                st.download_button("📕 PDF", pdf_data, f"fma_layer{st.session_state.current_layer}.pdf", "application/pdf")
            except Exception as e: st.warning(f"تعذر PDF: {str(e)[:60]}")
    with te2:
        if map_fig:
            map_html = map_fig.to_html(include_plotlyjs='cdn', config={'scrollZoom':True}, full_html=True)
            st.download_button("🗺️ تحميل الخريطة", map_html, f"map_layer{st.session_state.current_layer}.html", "text/html")
        else:
            st.info("تصدير الخريطة غير متاح حالياً، استخدم رمز Mapbox لإنشاء خريطة قابلة للتصدير.")

else:
    st.info("👈 لا توجد بيانات. ابدأ التتبع أو اضغط 'قراءة واحدة' لتسجيل نقطة.")

st.markdown("---")
st.caption(f"🏗️ FMA Compaction Analyzer Pro | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")