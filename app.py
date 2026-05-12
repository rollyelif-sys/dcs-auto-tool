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
# 小米大模型的兼容接口地址
BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1" 
# 调用小米全模态（包含视觉识图）模型
MODEL_NAME = "mimo-v2-omni" 
# ==============================================

# 设置网页基础属性
st.set_page_config(page_title="DCS自动巡检台", page_icon="🏭", layout="wide")

# ================= 安全与全局设置区 =================
with st.sidebar:
    st.header("🔑 系统与班次设置")
    # 使用密码框输入 API Key，保护隐私不外泄
    api_key_input = st.text_input("请输入小米 MiMo API Key", type="password", help="在此输入你的最新 Key，刷新网页会清空，确保安全。")
    
    # 班次选择，方便自动填入报表
    current_shift = st.selectbox("当前班次", ["白班", "夜班", "其他"])
    
    st.markdown("---")
    st.markdown("⚠️ **提示**：为防止泄露，请使用最新的 API Key。每次重新打开网页时需要输入一次。")

# ================= 初始化暂存池 (Session State) =================
# records 用于保存各个模板的数据
if "records" not in st.session_state:
    st.session_state.records = {}
# template_lens 用于记录模板最初的行数，以便计算新增了多少条
if "template_lens" not in st.session_state:
    st.session_state.template_lens = {}
# last_img 用于防止同一张图片被手滑重复提交
if "last_img" not in st.session_state:
    st.session_state.last_img = ""

st.title("🏭 DCS 自动巡检台 (工业级稳固版)")
st.markdown("💡 **操作流**：左侧填 Key 和班次 -> 上传空模板 -> 连续上传截图提取 -> 页面最下方打包下载。")

col1, col2 = st.columns([1, 1])

# 图像转 Base64 编码函数（大模型视觉 API 标准输入格式）
def encode_image_to_base64(img, max_size=1024, quality=80):
    """压缩图片后转 Base64，防止 413 Request Entity Too Large 错误"""
    img = img.convert('RGB')
    # 等比缩放，最长边不超过 max_size
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    # 压缩为 JPEG
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

with col1:
    st.subheader("📁 1. 基础文件上传")
    excel_file = st.file_uploader("📊 上传本产线的 Excel 空白模板", type=["xlsx", "xls"])
    image_file = st.file_uploader("📸 上传单对反应釜的 DCS 截图", type=["jpg", "jpeg", "png"])
    
    # 当上传了 Excel 模板时
    if excel_file:
        template_name = excel_file.name
        # 如果是该模板第一次上传，读取其结构并初始化到暂存池
        if template_name not in st.session_state.records:
            try:
                df_temp = pd.read_excel(excel_file, engine="openpyxl")
            except Exception as e1:
                try:
                    excel_file.seek(0)
                    df_temp = pd.read_excel(excel_file, engine="xlrd")
                except Exception as e2:
                    try:
                        excel_file.seek(0)
                        df_temp = pd.read_excel(excel_file)
                    except Exception as e3:
                        st.error(f"Excel读取失败，请确认文件格式正确（需为.xlsx或.xls）。错误：{{e3}}")
                        st.stop()
            st.session_state.records[template_name] = df_temp
            st.session_state.template_lens[template_name] = len(df_temp)
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
            # 校验是否输入了 API Key
            if not api_key_input:
                st.error("🚨 请先在左侧边栏输入 API Key！")
            else:
                # 防重复提交校验
                img_id = f"{image_file.name}_{image_file.size}"
                if st.session_state.last_img == img_id:
                    st.warning("⚠️ 这张截图刚刚已经成功提取过啦，请上传下一张新截图。")
                else:
                    with st.spinner(f"正在呼叫 {MODEL_NAME} 视觉大模型执行工艺数据比对，请稍候..."):
                        try:
                            # 初始化 OpenAI 客户端连接小米服务，设置超时防卡死
                            client = OpenAI(api_key=api_key_input, base_url=BASE_URL, timeout=120.0)
                            base64_image = encode_image_to_base64(image)
                            
                            # 将列名格式化为清晰的列表，提高 AI 识别准确率
                            formatted_columns = "\n".join(f"- {c}" for c in target_columns)
                            
                            # 核心提示词：明确区分a/b双管，逐个精确读取
                            prompt = f"""
                            你是一个资深的化工DCS巡检员。这是一个**双管反应釜**的中控截图，左侧是a管（主釜），右侧是b管（次釜），两套数据都必须读取。

                            【第一步：逐个读取屏幕上的实际值（PV）】
                            注意：PV（实际值）通常是绿底黑字的大号数字，SP（设定值）是旁边的小号数字。只读PV！

                            === a管（左侧主釜）===
                            - 金属液a流量: ___ L/h
                            - 液碱a流量: ___ L/h
                            - 氨水a流量: ___ L/h（可能显示为0.0，那就是0）
                            - 氮气a流量: ___ NL/m
                            - 空气a流量: ___ NL/m

                            === b管（右侧次釜）===
                            - 金属液b流量: ___ L/h
                            - 液碱b流量: ___ L/h
                            - 氨水b流量: ___ L/h
                            - 氮气b流量: ___ NL/m
                            - 空气b流量: ___ NL/m

                            === 釜体参数 ===
                            - pH（主釜）: ___（取较小值，忽略15左右的）
                            - 温度（主釜）: ___ ℃
                            - 温度（次釜）: ___ ℃
                            - 压力（主釜）: ___ kPa
                            - 转速（主釜）: ___ RPM
                            - 液位（次釜）: ___ m

                            【第二步：映射到模板列名】
                            模板列名（按顺序）：
                            {formatted_columns}

                            将上面读取的值，按列名含义匹配填入。key必须与模板列名完全一致。
                            无法从屏幕读取的管理字段（日期、时间、班次等）填空字符串""。

                            【输出】只输出JSON，不要任何解释。
                            """
                            
                            # 发送多模态请求
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
                                temperature=0.1 # 低温度值使输出更加确定和稳定
                            )
                            
                            result_text = response.choices[0].message.content.strip()
                            
                            # 修复截断Bug：使用 [`]{3} 代替连续的三个反引号，防止 Markdown 解析器误判截断
                            result_text = re.sub(r'^[`]{3}(?:json)?\s*|\s*[`]{3}$', '', result_text).strip()
                            data_dict = json.loads(result_text)
                            
                            # 稳妥填充日期、时间和班次（基于目标模板的列名遍历）
                            current_time = datetime.now()
                            for col in target_columns:
                                if "日期" in col:
                                    data_dict[col] = current_time.strftime("%Y-%m-%d")
                                elif "时间" in col and "填" not in col:
                                    data_dict[col] = current_time.strftime("%H:%M")
                                elif "班次" in col:
                                    data_dict[col] = current_shift
                            
                            # 转换为 DataFrame 并强制按目标模板列对齐（防止错位）
                            new_row_df = pd.DataFrame([data_dict]).reindex(columns=target_columns)
                            
                            # 追加进暂存池
                            st.session_state.records[template_name] = pd.concat(
                                [st.session_state.records[template_name], new_row_df], 
                                ignore_index=True
                            )
                            
                            # 记录最后成功提取的图片标记
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

# 遍历展示暂存池中的所有数据
if not st.session_state.records:
    st.info("暂存池空空如也，请在上方开始您的巡检提取。")
else:
    for t_name, df_accumulated in st.session_state.records.items():
        original_len = st.session_state.template_lens.get(t_name, 0)
        new_records_count = len(df_accumulated) - original_len
        
        st.subheader(f"📑 {t_name} (本班次已录入 {new_records_count} 组新数据)")
        st.dataframe(df_accumulated, use_container_width=True)
        
        # 将数据写入内存并准备下载
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
