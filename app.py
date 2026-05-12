import streamlit as st
from openai import OpenAI
import pandas as pd
import json
from PIL import Image
import io
import base64
from datetime import datetime
import re

# ================= 核心配置区 =================
BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
MODEL_NAME = "mimo-v2-omni"
# ==============================================

st.set_page_config(page_title="DCS自动巡检台", page_icon="🏭", layout="wide")

with st.sidebar:
    st.header("🔑 系统与班次设置")
    api_key_input = st.text_input("请输入小米 MiMo API Key", type="password", help="在此输入你的最新 Key，刷新网页会清空，确保安全。")
    current_shift = st.selectbox("当前班次", ["白班", "夜班", "其他"])
    st.markdown("---")
    st.markdown("⚠️ **提示**：为防止泄露，请使用最新的 API Key。每次重新打开网页时需要输入一次。")

if "records" not in st.session_state:
    st.session_state.records = []
if "last_img" not in st.session_state:
    st.session_state.last_img = ""

st.title("🏭 DCS 自动巡检台 (工业级稳固版)")
st.markdown("💡 **操作流**：设置列名 → 上传截图 → 提取数据 → 下方导出。")

col1, col2 = st.columns([1, 1])

def encode_image_to_base64(img, quality=85):
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def crop_region(img, box):
    """裁剪图片区域"""
    return img.crop(box)

# 默认模板列名
DEFAULT_COLUMNS = "金属液流量 A管,金属液流量 B管,碱液流量 A管,碱液流量 B管,氨水流量 单管进料,氮气流量 主釜,氮气流量 次釜,空气流量 主釜,空气流量 次釜,温度 主釜,温度 次釜,搅拌 主釜,搅拌 次釜,液位 次釜,主釜自循环流量,主次釜循环流量"

with col1:
    st.subheader("📋 1. 模板列名设置")
    columns_input = st.text_area(
        "输入模板列名（逗号分隔，按顺序）",
        value=DEFAULT_COLUMNS,
        height=120,
        help="从你的Excel模板第1行+第2行拼出列名，用逗号分隔。"
    )
    target_columns = [c.strip() for c in columns_input.split(",") if c.strip()]
    st.info(f"📌 当前共 **{len(target_columns)}** 列")

    st.subheader("📸 2. 上传DCS截图")
    image_file = st.file_uploader("上传单对反应釜的 DCS 截图", type=["jpg", "jpeg", "png"])

    if image_file:
        image = Image.open(image_file)
        st.image(image, caption="待提取的 DCS 屏幕截图", use_container_width=True)

with col2:
    st.subheader("⚙️ 3. AI 自动提取")

    if image_file:
        if st.button("🚀 提取数据并汇入暂存池", type="primary"):
            if not api_key_input:
                st.error("🚨 请先在左侧边栏输入 API Key！")
            else:
                img_id = f"{image_file.name}_{image_file.size}"
                if st.session_state.last_img == img_id:
                    st.warning("⚠️ 这张截图刚刚已经成功提取过啦，请上传下一张新截图。")
                else:
                    with st.spinner(f"正在呼叫 {MODEL_NAME} 视觉大模型，请稍候..."):
                        try:
                            client = OpenAI(api_key=api_key_input, base_url=BASE_URL, timeout=120.0)
                            w, h = image.size

                            # ============ 区域裁剪方案（SUPCON DCS专用）============
                            # 左侧=a管/主釜，右侧=b管/次釜
                            # ⚠️ 关键：每个区域严格限制在对应管路范围内，不能跨中线
                            regions = {
                                "a管流量": {
                                    "box": (0, int(h*0.2), int(w*0.35), int(h*0.55)),
                                    "prompt": "这是DCS截图【左侧a管】区域。\n请仔细区分以下三个流量计，分别读取PV值（绿底黑字，忽略Hz）：\n1. 金属液a（标注为金属液的流量计）= ___L/h\n2. 回流a（标注为回流的流量计，数值通常在200-230范围）= ___L/h\n3. 氨水a（标注为氨水的流量计）= ___L/h\n只输出JSON，如 {\"金属液a\":404.0,\"回流a\":223.6,\"氨水a\":98.2}"
                                },
                                "b管流量": {
                                    "box": (int(w*0.35), int(h*0.2), int(w*0.7), int(h*0.55)),
                                    "prompt": "这是DCS截图【右侧b管】区域。\n请仔细区分以下三个流量计，分别读取PV值（绿底黑字，忽略Hz）：\n1. 金属液b（标注为金属液的流量计）= ___L/h\n2. 回流b（标注为回流的流量计，数值通常在200-230范围）= ___L/h\n3. 氨水b（标注为氨水的流量计）= ___L/h\n只输出JSON，如 {\"金属液b\":404.5,\"回流b\":221.8,\"氨水b\":0.0}"
                                },
                                "主釜氮气空气": {
                                    "box": (0, int(h*0.05), int(w*0.35), int(h*0.4)),
                                    "prompt": "这是DCS截图【主釜/左侧】上方区域。\n请找到标注为'氮气'的流量计，读取其PV值（绿底黑字）。\n再找到标注为'空气'的流量计，读取其PV值。\n注意：只读主釜（左侧）的数据，忽略右侧次釜的数据。\n只输出JSON，如 {\"氮气主釜\":68.8,\"空气主釜\":0.0}"
                                },
                                "次釜氮气空气": {
                                    "box": (int(w*0.65), int(h*0.05), w, int(h*0.4)),
                                    "prompt": "这是DCS截图【次釜/右侧】上方区域。\n请找到标注为'氮气'的流量计，读取其PV值（绿底黑字）。\n再找到标注为'空气'的流量计，读取其PV值。\n注意：只读次釜（右侧）的数据，忽略左侧主釜的数据。\n只输出JSON，如 {\"氮气次釜\":151.7,\"空气次釜\":0.0}"
                                },
                                "主次釜面板": {
                                    "box": (0, int(h*0.45), int(w*0.5), h),
                                    "prompt": "读取主釜和次釜数据面板的所有参数（只读PV实际值，绿底黑字，忽略SP设定值）：\n主釜: pH值(取数值较小的那个，如10.456，忽略15左右的), 温度(℃), 液位(m), 转速(rpm)\n次釜: 温度(℃), 液位(m), 转速(rpm)\n注意：主釜液位和次釜液位都要读取！\n只输出JSON，如 {\"pH\":10.456,\"主釜温度\":69.46,\"次釜温度\":69.94,\"主釜液位\":2.5,\"次釜液位\":2.18,\"主釜转速\":164.7,\"次釜转速\":165.0}"
                                },
                                "罐体压力": {
                                    "box": (int(w*0.2), int(h*0.4), int(w*0.8), int(h*0.7)),
                                    "prompt": "读取主釜和次釜罐体上的压力值（只读PV实际值）：\n主釜压力=___kPa\n次釜压力=___kPa\n只输出JSON"
                                },
                                "循环流量": {
                                    "box": (int(w*0.15), int(h*0.5), int(w*0.85), int(h*0.85)),
                                    "prompt": "这是DCS截图中间区域，包含循环管道流量数据。\n请读取以下数值：\n自循环流量=___（标注为'自循环'或'自循环流'的流量计）\n主次釜循环=___（标注为'循环流量'或'主釜循环流量'的流量计）\n只输出JSON，如 {\"主釜自循环\":9.1,\"主次釜循环\":11.7}"
                                }
                            }

                            # 调用API识别每个区域
                            all_data = {}
                            progress = st.progress(0)
                            total = len(regions)
                            errors = []

                            for i, (name, region) in enumerate(regions.items()):
                                crop = crop_region(image, region["box"])
                                b64 = encode_image_to_base64(crop)

                                response = client.chat.completions.create(
                                    model=MODEL_NAME,
                                    messages=[{
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": region["prompt"]},
                                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                                        ]
                                    }],
                                    temperature=0.1
                                )

                                result = response.choices[0].message.content.strip()
                                result = re.sub(r'^[`]{3}(?:json)?\s*|\s*[`]{3}$', '', result).strip()

                                try:
                                    data = json.loads(result)
                                    all_data.update(data)
                                except json.JSONDecodeError:
                                    errors.append(f"区域 {name} 解析失败: {result[:100]}")

                                progress.progress((i + 1) / total)

                            # 显示解析失败的区域
                            for err in errors:
                                st.warning(err)

                            # ============ 合并结果 ============
                            st.subheader("📊 识别结果")
                            st.json(all_data)

                            # 统一同义词：回流→碱液→液碱，搅拌→转速
                            synonym_map = {
                                "回流": "碱液",
                                "碱液": "液碱",
                                "搅拌": "转速",
                            }

                            # 标准化值映射：将all_data的key统一
                            value_map = {}

                            # 直接复制已识别的值
                            for k, v in all_data.items():
                                if v is not None and v != "":
                                    value_map[k] = v

                            # 统一key名：区域prompt输出的key可能不同
                            # 氮气主釜/氮气次釜 → 氮气a/氮气b
                            key_aliases = {
                                "氮气主釜": "氮气a",
                                "氮气次釜": "氮气b",
                                "空气主釜": "空气a",
                                "空气次釜": "空气b",
                            }
                            for new_key, old_key in key_aliases.items():
                                if new_key in value_map and old_key not in value_map:
                                    value_map[old_key] = value_map[new_key]

                            # 生成同义词别名（递归，直到没有新key）
                            changed = True
                            while changed:
                                changed = False
                                for k, v in list(value_map.items()):
                                    for src, dst in synonym_map.items():
                                        if src in k:
                                            new_key = k.replace(src, dst)
                                            if new_key not in value_map:
                                                value_map[new_key] = v
                                                changed = True

                            # 补充常见变体
                            # "氨水 单管进料" → 取氨水a和氨水b中有效值（优先b）
                            if "氨水a" in value_map or "氨水b" in value_map:
                                a = value_map.get("氨水a", 0) or 0
                                b = value_map.get("氨水b", 0) or 0
                                value_map["氨水 单管进料"] = b if b > a else a
                                value_map["氨水单管进料"] = value_map["氨水 单管进料"]

                            # "主釜自循环" / "主次釜循环" 通常在DCS上没有独立读数，留空即可
                            # 如果用户模板有这些列，会留空，不会报错

                            # 填入模板列
                            current_time = datetime.now()
                            new_row = {}

                            for col in target_columns:
                                if "日期" in col:
                                    new_row[col] = current_time.strftime("%Y-%m-%d")
                                elif "时间" in col and "填" not in col:
                                    new_row[col] = current_time.strftime("%H:%M")
                                elif "班次" in col:
                                    new_row[col] = current_shift
                                elif "反应时间" in col:
                                    new_row[col] = ""
                                else:
                                    matched = False
                                    # 标准化当前列名，去掉"流量"等通用词
                                    col_norm = col
                                    for src, dst in synonym_map.items():
                                        col_norm = col_norm.replace(src, dst)

                                    # 精确匹配
                                    for key, val in value_map.items():
                                        if not val and val != 0:
                                            continue
                                        if key in col_norm or col_norm in key:
                                            new_row[col] = val
                                            matched = True
                                            break

                                    if not matched:
                                        # 模糊匹配
                                        if "金属液" in col and "A" in col:
                                            new_row[col] = value_map.get("金属液a", "")
                                        elif "金属液" in col and "B" in col:
                                            new_row[col] = value_map.get("金属液b", "")
                                        elif ("碱液" in col or "液碱" in col) and "A" in col:
                                            new_row[col] = value_map.get("液碱a", "")
                                        elif ("碱液" in col or "液碱" in col) and "B" in col:
                                            new_row[col] = value_map.get("液碱b", "")
                                        elif "氨" in col and "单管" in col:
                                            a = value_map.get("氨水a", 0) or 0
                                            b = value_map.get("氨水b", 0) or 0
                                            new_row[col] = b if b > a else a
                                        elif "氨" in col and "A" in col:
                                            new_row[col] = value_map.get("氨水a", "")
                                        elif "氨" in col and "B" in col:
                                            new_row[col] = value_map.get("氨水b", "")
                                        elif "氮" in col and "主" in col:
                                            new_row[col] = value_map.get("氮气a", "")
                                        elif "氮" in col and "次" in col:
                                            new_row[col] = value_map.get("氮气b", "")
                                        elif "空气" in col:
                                            # 空气流量为0时输出"/"
                                            val = ""
                                            if "主" in col:
                                                val = value_map.get("空气a", "")
                                            elif "次" in col:
                                                val = value_map.get("空气b", "")
                                            new_row[col] = "/" if (not val or val == 0) else val
                                        elif "温度" in col and "主" in col:
                                            new_row[col] = value_map.get("主釜温度", "")
                                        elif "温度" in col and "次" in col:
                                            new_row[col] = value_map.get("次釜温度", "")
                                        elif "搅拌" in col and "主" in col:
                                            new_row[col] = value_map.get("主釜转速", "")
                                        elif "搅拌" in col and "次" in col:
                                            new_row[col] = value_map.get("次釜转速", "")
                                        elif "液位" in col and "主" in col:
                                            new_row[col] = value_map.get("主釜液位", "")
                                        elif "液位" in col and "次" in col:
                                            new_row[col] = value_map.get("次釜液位", "")
                                        elif "液位" in col:
                                            new_row[col] = value_map.get("次釜液位", "")
                                        elif "自循环" in col and "主" in col:
                                            new_row[col] = value_map.get("主釜自循环", "")
                                        elif "自循环" in col:
                                            new_row[col] = value_map.get("次釜自循环", "")
                                        elif "主次釜循环" in col:
                                            new_row[col] = value_map.get("主次釜循环", "")
                                        elif "pH" in col.upper() or "PH" in col.upper():
                                            new_row[col] = value_map.get("pH", "")
                                        elif "压力" in col and "主" in col:
                                            new_row[col] = value_map.get("主釜压力", "")
                                        elif "压力" in col and "次" in col:
                                            new_row[col] = value_map.get("次釜压力", "")
                                        else:
                                            new_row[col] = ""

                            st.session_state.records.append(new_row)
                            st.session_state.last_img = img_id
                            st.success("✅ 提取成功！")

                        except Exception as e:
                            st.error(f"错误：{e}")
    else:
        st.info("请先在左侧上传 DCS 截图")

# ================= 底部：数据打包区 =================
st.divider()
st.header("🗄️ 下班前打包区 (今日总览)")

if not st.session_state.records:
    st.info("暂存池空空如也，请在上方开始您的巡检提取。")
else:
    df = pd.DataFrame(st.session_state.records)
    st.subheader(f"📑 已录入 {len(df)} 组数据")
    st.dataframe(df, use_container_width=True)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='今日巡检')

    st.download_button(
        label="📥 导出 Excel",
        data=output.getvalue(),
        file_name=f"巡检汇总_{datetime.now().strftime('%m月%d日')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
