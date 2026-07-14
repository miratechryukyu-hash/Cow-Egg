import streamlit as st
import pandas as pd
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="動物遠隔診療MVP", layout="wide")
st.title("動物遠隔診療サポートシステム")

# Googleスプレッドシートへの接続設定
conn = st.connection("gsheets", type=GSheetsConnection)

tab1, tab2 = st.tabs(["現場からの報告フォーム", "獣医師用ダッシュボード"])

# -------------------------------------------------------------------------
# タブ1: 現場からの報告
# -------------------------------------------------------------------------
with tab1:
    st.header("現場報告入力")
    
    # clear_on_submit=False に設定（送信後やEnterキーでの文字消えを防止）
    with st.form("report_form", clear_on_submit=False):
        reporter = st.text_input("報告者名")
        animal_id = st.text_input("個体識別番号 / 名前")
        temperature = st.number_input("測定体温 (C)", min_value=30.0, max_value=45.0, value=38.5, step=0.1)
        
        symptoms = st.multiselect(
            "主な症状（複数選択可）",
            ["食欲不振", "歩行異常", "出血", "下痢・嘔吐", "ぐったりしている", "その他"]
        )
        
        # 【追加】誤送信防止チェックボックス
        confirm_send = st.checkbox("すべての入力が完了しました（チェックを入れてから送信）")
        
        submit_button = st.form_submit_button("報告を送信する")
        
        if submit_button:
            if not confirm_send:
                st.warning("誤送信を防ぐため、「すべての入力が完了しました」にチェックを入れてから送信ボタンを押してください。")
            elif not reporter or not animal_id:
                st.error("報告者名と個体識別番号は必須です。")
            else:
                triage = "低・定例報告"
                if temperature >= 40.0 or "出血" in symptoms or "ぐったりしている" in symptoms:
                    triage = "高・即時相談"
                elif temperature >= 39.3 or len(symptoms) > 0:
                    triage = "中・要観察"
                
                new_data = pd.DataFrame([{
                    "記録ID": str(int(datetime.now().timestamp())),
                    "日時": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "報告者名": reporter,
                    "個体識別番号": animal_id,
                    "体温": temperature,
                    "主な症状": ", ".join(symptoms),
                    "患部写真": "写真なし",
                    "トリアージ判定": triage,
                    "確認ステータス": "未確認",
                    "獣医師コメント": ""
                }])
                
                try:
                    existing_data = conn.read(worksheet="問診記録", ttl=0)
                    updated_data = pd.concat([existing_data, new_data], ignore_index=True)
                    conn.update(worksheet="問診記録", data=updated_data)
                    st.success(f"スプレッドシートへの送信が完了しました。 判定結果: {triage}")
                except Exception as e:
                    st.error(f"送信エラーが発生しました: {e}")

# -------------------------------------------------------------------------
# タブ2: 獣医師用ダッシュボード
# -------------------------------------------------------------------------
with tab2:
    st.header("未対応の報告一覧")
    
    try:
        df = conn.read(worksheet="問診記録", ttl=0)
        df = df.fillna("") # 空白データによるエラーを防止
        
        unconfirmed_df = df[df["確認ステータス"] == "未確認"]
        
        if unconfirmed_df.empty:
            st.info("現在、未対応の報告はありません。")
        else:
            # 【追加】プルダウンで対応する報告を選択する機能
            indices = unconfirmed_df.index.tolist()
            
            def format_option(idx):
                row = unconfirmed_df.loc[idx]
                return f"{row['日時']} - 個体: {row['個体識別番号']} (報告者: {row['報告者名']})"
            
            selected_idx = st.selectbox("対応する報告を選択してください", indices, format_func=format_option)
            
            if selected_idx is not None:
                row = unconfirmed_df.loc[selected_idx]
                
                with st.container():
                    st.markdown(f"### 【{row['トリアージ判定']}】 個体: {row['個体識別番号']}")
                    st.write(f"**報告日時:** {row['日時']} | **報告者:** {row['報告者名']}")
                    st.write(f"**体温:** {row['体温']} C")
                    st.write(f"**症状:** {row['主な症状']}")
                    
                    # 返信用のフォーム
                    with st.form(key=f"vet_form_{selected_idx}"):
                        comment = st.text_area("指示・コメントを入力")
                        submit_reply = st.form_submit_button("対応完了にする")
                        
                        if submit_reply:
                            df.at[selected_idx, "確認ステータス"] = "対応完了"
                            df.at[selected_idx, "獣医師コメント"] = comment
                            conn.update(worksheet="問診記録", data=df)
                            st.success("ステータスを更新しました。")
                            st.rerun()
                            
    except Exception as e:
        st.error(f"データ読み込みエラー: {e}")
