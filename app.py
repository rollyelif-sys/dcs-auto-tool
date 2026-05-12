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
你是一个资深的化工DCS巡检员。请从SUPCON双管反应釜中控截图中精确提取数据。

【关键规则】
1. 每个流量计的数值框里有多行数字，你必须读取第一行（PV实际值），不是第二行（SP设定值），不是第三行（频率），不是底部的累计数！
2. PV值通常是较大的数字，带单位L/h或NL/m
3. 累计数通常较小或带m³单位，在数值框底部，不要读！
4. 数据面板在屏幕左侧下方，分"主釜数据"和"次釜数据"两个面板

【第一步：逐个读取PV值】

=== a管流量（左侧管路，从上往下）===
在屏幕左侧找到标有"金属液""液碱""氨水"的管路，每根管路上有一个数值框：
- 金属液a = 数值框第一行PV值 ___ L/h
- 液碱a = 数值框第一行PV值 ___ L/h（不是底部的累计数！）
- 氨水a = 数值框第一行PV值 ___ L/h（可能是0）

=== b管流量（中间/右侧管路，从上往下）===
- 金属液b = 数值框第一行PV值 ___ L/h
- 液碱b = 数值框第一行PV值 ___ L/h
- 氨水b = 数值框第一行PV值 ___ L/h

=== 氮气流量（找到标"氮气"的管路）===
- 氮气a = 数值框第一行PV值 ___ NL/m
- 氮气b = 数值框第一行PV值 ___ NL/m

=== 空气流量（找到标"空气"的管路）===
- 空气a = 数值框第一行PV值 ___ NL/m
- 空气b = 数值框第一行PV值 ___ NL/m

=== 主釜数据面板（屏幕左侧下方，标"主釜数据"的区域）===
- pH = ___（有两个通道，取较小值，忽略15左右的）
- 主釜温度 = ___ ℃
- 主釜液位 = ___ m
- 主釜转速 = ___ rpm

=== 次釜数据面板（屏幕左侧下方，标"次釜数据"的区域）===
- 次釜温度 = ___ ℃
- 次釜液位 = ___ m
- 次釜转速 = ___ rpm

=== 罐体参数 ===
- 主釜压力 = ___ kPa（主釜罐体上）
- 次釜压力 = ___ kPa（次釜罐体上）

=== 自循环/循环流量 ===
- 主釜自循环流量 = ___ L/h
- 次釜自循环流量 = ___ L/h
- 主次釜循环流量 = ___ L/h

【第二步：填入Excel模板】

模板列名（按顺序）：
{formatted_columns}

同义词：模板"碱液"=DCS"液碱"，模板"搅拌"=DCS"转速"

逐列匹配：
- 含"金属液"且含"A" → 金属液a
- 含"金属液"且含"B" → 金属液b
- 含"碱液"且含"A" → 液碱a
- 含"碱液"且含"B" → 液碱b
- 含"氨"且含"主" → 氨水a
- 含"氨"且含"次" → 氨水b
- 含"氨"且无主次 → 看DCS实际进料情况
- 含"氮"且含"主" → 氮气a
- 含"氮"且含"次" → 氮气b
- 含"空气"且含"主" → 空气a
- 含"空气"且含"次" → 空气b
- 含"温度"且含"主" → 主釜温度
- 含"温度"且含"次" → 次釜温度
- 含"搅拌"且含"主" → 主釜转速
- 含"搅拌"且含"次" → 次釜转速
- 含"液位" → 次釜液位
- 含"自循环"且含"主" → 主釜自循环流量
- 含"自循环"且含"次" → 次釜自循环流量
- 含"主次釜循环" → 主次釜循环流量
- 含"pH" → pH值
- 含"日期"或"时间"或"反应时间" → ""

【输出】只输出JSON，key与模板列名完全一致，value填纯数字或空字符串。
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
