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

# ================= 安全与全局设置区 =================
with st.sidebar:
    st.header("🔑 系统与班次设置")
    api_key_input = st.text_input("请输入小米 MiMo API Key", type="password", help="在此输入你的最新 Key，刷新网页会清空，确保安全。")
    current_shift = st.selectbox("当前班次", ["白班", "夜班", "其他"])
    st.markdown("---")
    st.markdown("⚠️ **提示**：为防止泄露，请使用最新的 API Key。每次重新打开网页时需要输入一次。")

# ================= 初始化暂存池 =================
if "records" not in st.session_state:
    st.session_state.records = {}
if "template_lens" not in st.session_state:
    st.session_state.template_lens = {}
if "last_img" not in st.session_state:
    st.session_state.last_img = ""

st.title("🏭 DCS 自动巡检台 (工业级稳固版)")
st.markdown("💡 **操作流**：左侧填 Key 和班次 -> 上传空模板 -> 连续上传截图提取 -> 页面最下方打包下载。")

col1, col2 = st.columns([1, 1])

# 图像转 Base64 编码函数（压缩防413）
def encode_image_to_base64(img, max_size=1024, quality=80):
    """压缩图片后转 Base64，防止 413 Request Entity Too Large 错误"""
    img = img.convert('RGB')
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

# 读取Excel模板（支持合并表头）
def read_excel_template(file):
    """读取Excel模板，自动处理合并表头（第1行大类+第2行子列）"""
    for engine in ["openpyxl", "xlrd"]:
        try:
            file.seek(0)
            df = pd.read_excel(file, engine=engine, header=[0, 1])
            df.columns = [
                ' '.join(str(s).strip() for s in col if str(s).strip() and str(s) != 'nan')
                for col in df.columns
            ]
            df = df.dropna(how='all').reset_index(drop=True)
            return df
        except Exception:
            continue
    # 最后尝试无合并表头
    file.seek(0)
    df = pd.read_excel(file)
    df = df.dropna(how='all').reset_index(drop=True)
    return df

with col1:
    st.subheader("📁 1. 基础文件上传")
    excel_file = st.file_uploader("📊 上传本产线的 Excel 空白模板", type=["xlsx", "xls"])
    image_file = st.file_uploader("📸 上传单对反应釜的 DCS 截图", type=["jpg", "jpeg", "png"])

    if excel_file:
        template_name = excel_file.name
        if template_name not in st.session_state.records:
            try:
                df_temp = read_excel_template(excel_file)
                st.session_state.records[template_name] = df_temp
                st.session_state.template_lens[template_name] = len(df_temp)
            except Exception as e:
                st.error(f"Excel读取失败：{e}")
                st.stop()
        st.success(f"📌 当前正在处理模板：**{template_name}**")

# 当模板和截图都就绪时，展示提取操作区
if excel_file and image_file:
    template_name = excel_file.name
    target_columns = st.session_state.records[template_name].columns.tolist()

    with col1:
        image = Image.open(image_file)
        st.image(image, caption="待提取的 DCS 屏幕截图", use_container_width=True)

    with col2:
        st.subheader("⚙️ 2. AI 自动提取")

        if st.button("🚀 提取数据并汇入暂存池", type="primary"):
            if not api_key_input:
                st.error("🚨 请先在左侧边栏输入 API Key！")
            else:
                img_id = f"{image_file.name}_{image_file.size}"
                if st.session_state.last_img == img_id:
                    st.warning("⚠️ 这张截图刚刚已经成功提取过啦，请上传下一张新截图。")
                else:
                    with st.spinner(f"正在呼叫 {MODEL_NAME} 视觉大模型执行工艺数据比对，请稍候..."):
                        try:
                            client = OpenAI(api_key=api_key_input, base_url=BASE_URL, timeout=120.0)
                            base64_image = encode_image_to_base64(image)

                            formatted_columns = "\n".join(f"- {c}" for c in target_columns)

                            prompt = f"""
你是一个资深的化工DCS巡检员。请从这张SUPCON双管反应釜中控截图中精确提取数据。

【读取规则】
- 双管反应釜：左侧a管（主釜），右侧b管（次釜）
- 只读PV实际值（绿底黑字大号数字），不读SP设定值（旁边小号数字）
- 流量计旁通常有三行：第一行PV，第二行SP，第三行频率。只取第一行PV！
- pH有两个通道，取较小值（忽略15左右的异常值）
- 温度有两个通道，都读取

【第一步：逐个读取以下所有PV值，不要遗漏任何一个】

=== a管（左侧主釜）管路流量 ===
- 金属液a = ___ （单位L/h）
- 液碱a = ___ （单位L/h，DCS上标"液碱"）
- 氨水a = ___ （单位L/h，可能显示0.0，那就是0）
- 氮气a = ___ （单位NL/m，DCS上标"氮气"）
- 空气a = ___ （单位NL/m）

=== b管（右侧次釜）管路流量 ===
- 金属液b = ___ （单位L/h）
- 液碱b = ___ （单位L/h）
- 氨水b = ___ （单位L/h）
- 氮气b = ___ （单位NL/m）
- 空气b = ___ （单位NL/m）

=== 主釜数据面板（屏幕左侧"主釜数据"区域）===
- pH = ___ （两个通道取较小的，忽略15左右的）
- 主釜温度 = ___ （单位℃）
- 主釜液位 = ___ （单位m）
- 主釜转速 = ___ （单位rpm或Hz）
- 主釜功率 = ___ （单位kW）

=== 次釜数据面板（屏幕左侧"次釜数据"区域）===
- 次釜温度 = ___ （单位℃）
- 次釜液位 = ___ （单位m）
- 次釜转速 = ___ （单位rpm或Hz）
- 次釜功率 = ___ （单位kW）

=== 罐体参数 ===
- 主釜压力 = ___ （单位kPa，主釜罐体上显示）
- 次釜压力 = ___ （单位kPa，次釜罐体上显示）

=== 自循环/循环流量 ===
- 主釜自循环流量 = ___ （单位L/h，主釜旁的自循环管路）
- 次釜自循环流量 = ___ （单位L/h，次釜旁的自循环管路）
- 主次釜循环流量 = ___ （单位L/h，主釜到次釜之间的循环管路）

【第二步：填入Excel模板】

模板列名（按顺序）：
{formatted_columns}

同义词对照：
- 模板"碱液" = DCS"液碱"
- 模板"搅拌" = DCS"转速"
- 模板"A管" = DCS"a管" = 主釜侧
- 模板"B管" = DCS"b管" = 次釜侧
- 模板"主釜" = DCS"主釜"
- 模板"次釜" = DCS"次釜"
- 模板"氨气" = DCS上无此数据（氨气NH₃≠氮气N₂≠氨水NH₃·H₂O，三者完全不同）

逐列匹配规则：
- 含"金属液"且含"A"或"a" → 金属液a
- 含"金属液"且含"B"或"b" → 金属液b
- 含"碱液"或"液碱"且含"A"或"a" → 液碱a
- 含"碱液"或"液碱"且含"B"或"b" → 液碱b
- 含"氨水" → 如果是单列（无A/B），取氨水a和氨水b中有实际流量的那个值；如果都为0则填0
- 含"氨气"且含"主" → DCS上无氨气数据，填""
- 含"氨气"且含"次" → DCS上无氨气数据，填""
- 含"空气"且含"主"或"a"或"A" → 空气a
- 含"空气"且含"次"或"b"或"B" → 空气b
- 含"温度"且含"主" → 主釜温度
- 含"温度"且含"次" → 次釜温度
- 含"搅拌"或"转速"且含"主" → 主釜转速
- 含"搅拌"或"转速"且含"次" → 次釜转速
- 含"液位" → 次釜液位（如果模板只有次釜液位列）
- 含"自循环"且含"主" → 主釜自循环流量
- 含"自循环"且含"次" → 次釜自循环流量
- 含"主次釜循环" → 主次釜循环流量
- 含"pH"或"PH" → pH值
- 含"日期"或"时间"或"反应时间" → ""

如果列名无法匹配任何规则，填""。

【输出】只输出JSON。key必须与模板列名完全一致。value填纯数字（无单位）或空字符串。绝对不要markdown标记。
"""

                            response = client.chat.completions.create(
                                model=MODEL_NAME,
                                messages=[
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": prompt},
                                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                                        ]
                                    }
                                ],
                                temperature=0.1
                            )

                            result_text = response.choices[0].message.content.strip()
                            result_text = re.sub(r'^[`]{3}(?:json)?\\s*|\\s*[`]{3}$', '', result_text).strip()
                            data_dict = json.loads(result_text)

                            current_time = datetime.now()
                            for col in target_columns:
                                if "日期" in col:
                                    data_dict[col] = current_time.strftime("%Y-%m-%d")
                                elif "时间" in col and "填" not in col:
                                    data_dict[col] = current_time.strftime("%H:%M")
                                elif "班次" in col:
                                    data_dict[col] = current_shift

                            new_row_df = pd.DataFrame([data_dict]).reindex(columns=target_columns)

                            st.session_state.records[template_name] = pd.concat(
                                [st.session_state.records[template_name], new_row_df],
                                ignore_index=True
                            )

                            st.session_state.last_img = img_id
                            st.success("✅ 提取成功！已自动汇入下方暂存池。请继续上传下一张图片。")

                        except json.JSONDecodeError:
                            st.error("解析失败：AI 返回的数据格式异常。这通常是由于图片过于模糊导致。")
                            st.code(result_text)
                        except Exception as e:
                            st.error(f"网络、接口或超时错误：{e}")

# ================= 底部：数据打包区 =================
st.divider()
st.header("🗄️ 下班前打包区 (今日总览)")

if not st.session_state.records:
    st.info("暂存池空空如也，请在上方开始您的巡检提取。")
else:
    for t_name, df_accumulated in st.session_state.records.items():
        original_len = st.session_state.template_lens.get(t_name, 0)
        new_records_count = len(df_accumulated) - original_len

        st.subheader(f"📑 {t_name} (本班次已录入 {new_records_count} 组新数据)")
        st.dataframe(df_accumulated, use_container_width=True)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df_accumulated.to_excel(writer, index=False, sheet_name='今日巡检')

        st.download_button(
            label=f"📥 导出完整版【{t_name}】",
            data=output.getvalue(),
            file_name=f"巡检汇总_{datetime.now().strftime('%m月%d日')}_{t_name}",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"download_{t_name}"
        )
