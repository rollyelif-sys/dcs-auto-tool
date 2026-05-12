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

def encode_image_to_base64(img, max_size=1024, quality=80):
    img = img.convert('RGB')
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

# 默认模板列名
DEFAULT_COLUMNS = "金属液流量 A管,金属液流量 B管,碱液流量 A管,碱液流量 B管,氨水流量 单管进料,氮气流量 主釜,氮气流量 次釜,空气流量 主釜,空气流量 次釜,温度 主釜,温度 次釜,搅拌 主釜,搅拌 次釜,液位 次釜,主釜自循环流量,主次釜循环流量"

with col1:
    st.subheader("📋 1. 模板列名设置")
    columns_input = st.text_area(
        "输入模板列名（逗号分隔，按顺序）",
        value=DEFAULT_COLUMNS,
        height=120,
        help="从你的Excel模板第1行+第2行拼出列名，用逗号分隔。例如：金属液流量 A管,碱液流量 A管"
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
                            base64_image = encode_image_to_base64(image)
                            formatted_columns = "\n".join(f"- {c}" for c in target_columns)

                            prompt = f"""你是化工DCS巡检员，从SUPCON双管反应釜截图提取数据填入Excel。

【DCS界面布局】
■ 左侧a管管路流量（从上到下）：金属液a、液碱a、氨水a
  每个流量计的数值框有三行：
    第一行（大字）= PV实际值（L/h）← 只读这个！
    第二行（小字）= SP设定值 ← 不读
    第三行 = 频率Hz ← 不读
    底部更小的字 = 累计数（m³）← 绝对不读！
  如果数值是另一个的几倍大（如723 vs 223），你读到了累计数，请重读第一行！

■ 右侧b管管路流量：金属液b、液碱b、氨水b（读法同上）

■ 氮气管路（标签"氮气"）：氮气a、氮气b（PV值，NL/m）
■ 空气管路（标签"空气"）：空气a、空气b（PV值，NL/m）

■ 主釜数据面板（屏幕左侧下方，标"主釜数据"）：
  pH值1（约15，不用）、pH值2（较小，用这个）
  温度1、温度2、液位、功率、转速

■ 次釜数据面板（标"次釜数据"）：温度、液位、功率、转速
  注意：次釜温度在"次釜数据"面板里，不要读成主釜的！

■ 罐体：主釜压力(kPa)、次釜压力(kPa)
■ 自循环/循环流量

【读取以下PV值】
a管: 金属液a=___, 液碱a=___, 氨水a=___
b管: 金属液b=___, 液碱b=___, 氨水b=___
氮气: 氮气a=___, 氮气b=___
空气: 空气a=___, 空气b=___
主釜面板: pH=较小值___, 主釜温度=___, 主釜液位=___, 主釜转速=___
次釜面板: 次釜温度=___, 次釜液位=___, 次釜转速=___
罐体: 主釜压力=___, 次釜压力=___
循环: 主釜自循环=___, 次釜自循环=___, 主次釜循环=___

【填入模板】列名: {formatted_columns}
同义词: "碱液"="液碱", "搅拌"="转速"
- 含"金属液"且含"A"->金属液a | 含"金属液"且含"B"->金属液b
- 含"碱液"且含"A"->液碱a | 含"碱液"且含"B"->液碱b
- 含"氨"且含"单管"->看实际哪个管有流量 | 含"氨"且含"主"->氨水a | 含"氨"且含"次"->氨水b
- 含"氮"且含"主"->氮气a | 含"氮"且含"次"->氮气b
- 含"空气"且含"主"->空气a | 含"空气"且含"次"->空气b
- 含"温度"且含"主"->主釜温度 | 含"温度"且含"次"->次釜温度
- 含"搅拌"且含"主"->主釜转速 | 含"搅拌"且含"次"->次釜转速
- 含"液位"->次釜液位
- 含"自循环"->主釜自循环 | 含"主次釜循环"->主次釜循环
- 含"pH"->pH值(取小的)
无法匹配->""
只输出JSON, key与列名完全一致, value纯数字或空字符串。"""

                            response = client.chat.completions.create(
                                model=MODEL_NAME,
                                messages=[{
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": prompt},
                                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                                    ]
                                }],
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

                            new_row = {col: data_dict.get(col, "") for col in target_columns}
                            st.session_state.records.append(new_row)
                            st.session_state.last_img = img_id
                            st.success("✅ 提取成功！")

                            # 显示结果
                            st.json(data_dict)

                        except json.JSONDecodeError:
                            st.error("解析失败：AI 返回的数据格式异常。")
                            st.code(result_text)
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
