import os
import csv
import sys
import argparse

# Import normalization rules from app.py
try:
    from app import normalize_name, normalize_division, normalize_racket
except ImportError:
    print("[ERROR] app.py를 찾을 수 없습니다. 스크립트를 프로젝트 루트 폴더에서 실행해 주세요.")
    sys.exit(1)

def get_gspread_client():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        if os.path.exists("credentials.json"):
            cred_path = "credentials.json"
            
    if not cred_path or not os.path.exists(cred_path):
        raise FileNotFoundError("구글 API 인증용 credentials.json 파일을 찾을 수 없습니다. 프로젝트 루트에 배치해 주세요.")
        
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(cred_path, scope)
        return gspread.authorize(creds)
    except ImportError:
        raise ImportError("필수 라이브러리(gspread, oauth2client)가 가상환경에 설치되어 있지 않습니다. pip install -r requirements.txt를 실행해 주세요.")

def parse_csv(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"지정한 CSV 파일 '{file_path}'을 찾을 수 없습니다.")
        
    cleaned_rows = []
    
    # UTF-8 with BOM 인코딩을 고려하여 utf-8-sig로 파일 읽기
    with open(file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        
        # 헤더 필드 확인
        headers = reader.fieldnames
        if not headers:
            raise ValueError("CSV 파일이 비어있거나 헤더가 누락되었습니다.")
            
        # 컬럼 매핑 (사양: 이름, 부수, 라켓)
        name_col = next((h for h in headers if '이름' in h or 'name' in h.lower()), None)
        div_col = next((h for h in headers if '부수' in h or 'division' in h.lower()), None)
        racket_col = next((h for h in headers if '라켓' in h or 'racket' in h.lower()), None)
        
        if not name_col or not div_col or not racket_col:
            raise ValueError(f"CSV 파일에 필수 컬럼(이름, 부수, 라켓)이 없습니다. (감지된 헤더: {headers})")
            
        for line_num, row in enumerate(reader, start=2):
            name_val = row.get(name_col, "").strip()
            div_val = row.get(div_col, "").strip()
            racket_val = row.get(racket_col, "").strip()
            
            # 빈 행은 건너뜀
            if not name_val and not div_val and not racket_val:
                continue
                
            # '상위부', '중위부', '하위부' 등 구획선 역할을 하는 행 건너뜀
            if ('부' in name_val) and not div_val and not racket_val:
                continue
                
            # 정규화 모듈 가동
            cleaned_rows.append({
                "이름": normalize_name(name_val),
                "부수": normalize_division(div_val),
                "라켓": normalize_racket(racket_val)
            })
            
    return cleaned_rows

def run_upload(file_path, mode):
    members = parse_csv(file_path)
    total_count = len(members)
    if total_count == 0:
        return 0, "업로드할 유효한 데이터가 없습니다."
        
    client = get_gspread_client()
    spreadsheet = client.open("탁우회_명단")
    sheet = spreadsheet.sheet1
    
    rows_to_upload = [[m["이름"], m["부수"], m["라켓"]] for m in members]
    
    if mode == "overwrite":
        sheet.clear()
        sheet.append_rows([["이름", "부수", "라켓"]] + rows_to_upload)
    else:
        sheet.append_rows(rows_to_upload)
        
    return total_count, None

# Tkinter Desktop GUI Mode
def start_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    
    root = tk.Tk()
    root.title("POWERDRIVE RANK - 회원 데이터 업로드")
    root.geometry("540x290")
    root.resizable(False, False)
    root.configure(bg="#0f172a")  # Dark Theme bg slate-900
    
    # Theme configuration
    style = ttk.Style()
    style.theme_use('clam')
    style.configure("TLabel", background="#0f172a", foreground="#f8fafc", font=("NanumGothic", 10))
    style.configure("TRadiobutton", background="#0f172a", foreground="#cbd5e1", font=("NanumGothic", 10))
    
    # Title Label
    title_label = tk.Label(root, text="POWERDRIVE RANK 업로드 도구", font=("NanumGothic", 15, "bold"), bg="#0f172a", fg="#818cf8")
    title_label.pack(pady=15)
    
    # File Select Frame
    file_frame = tk.Frame(root, bg="#0f172a")
    file_frame.pack(fill="x", padx=25, pady=10)
    
    file_entry_var = tk.StringVar()
    file_entry = tk.Entry(file_frame, textvariable=file_entry_var, width=40, font=("NanumGothic", 10), state="readonly")
    file_entry.pack(side="left", padx=5)
    
    def browse_file():
        file_selected = filedialog.askopenfilename(
            title="회원 명단 CSV 파일 선택",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")]
        )
        if file_selected:
            file_entry_var.set(file_selected)
            
    browse_btn = tk.Button(file_frame, text="파일 찾기", command=browse_file, bg="#312e81", fg="#f8fafc", activebackground="#4338ca", activeforeground="#ffffff", font=("NanumGothic", 9, "bold"), padx=10, pady=2, bd=0)
    browse_btn.pack(side="right", padx=5)
    
    # Mode Select Frame
    mode_frame = tk.LabelFrame(root, text=" 업로드 모드 설정 ", font=("NanumGothic", 9, "bold"), bg="#0f172a", fg="#818cf8", bd=1, relief="solid")
    mode_frame.pack(fill="x", padx=25, pady=15)
    
    mode_var = tk.StringVar(value="overwrite")
    
    overwrite_btn = tk.Radiobutton(
        mode_frame, 
        text="덮어쓰기 (기존 명단 전체 대체 - Overwrite)", 
        variable=mode_var, 
        value="overwrite",
        bg="#0f172a",
        fg="#cbd5e1",
        selectcolor="#1e293b",
        activebackground="#0f172a",
        activeforeground="#ffffff",
        font=("NanumGothic", 10),
        bd=0
    )
    overwrite_btn.pack(anchor="w", padx=15, pady=5)
    
    append_btn = tk.Radiobutton(
        mode_frame, 
        text="이어붙이기 (기존 데이터 아래에 추가 - Append)", 
        variable=mode_var, 
        value="append",
        bg="#0f172a",
        fg="#cbd5e1",
        selectcolor="#1e293b",
        activebackground="#0f172a",
        activeforeground="#ffffff",
        font=("NanumGothic", 10),
        bd=0
    )
    append_btn.pack(anchor="w", padx=15, pady=5)
    
    # Execute Button
    def on_upload_click():
        file_path = file_entry_var.get()
        if not file_path:
            messagebox.showwarning("경고", "업로드할 CSV 파일을 먼저 선택해 주세요.")
            return
            
        mode = mode_var.get()
        confirm = messagebox.askyesno(
            "업로드 확인", 
            f"선택한 파일을 [{mode.upper()}] 모드로 구글 시트에 정말 업로드하시겠습니까?"
        )
        if not confirm:
            return
            
        try:
            upload_btn.config(state="disabled", text="업로드 중...")
            root.update()
            
            count, error = run_upload(file_path, mode)
            
            if error:
                messagebox.showerror("오류", error)
            else:
                messagebox.showinfo("성공", f"성공적으로 업로드 완료했습니다!\n(반영된 부원 수: {count}명)")
                root.destroy() # Close window on success
                
        except Exception as ex:
            messagebox.showerror("오류 발생", f"업로드 중 에러가 발생했습니다:\n{ex}")
        finally:
            try:
                if root.winfo_exists():
                    upload_btn.config(state="normal", text="구글 시트 업로드 시작")
            except Exception:
                pass
            
    upload_btn = tk.Button(root, text="구글 시트 업로드 시작", command=on_upload_click, bg="#4f46e5", fg="#ffffff", activebackground="#6366f1", activeforeground="#ffffff", font=("NanumGothic", 11, "bold"), pady=8, bd=0, cursor="hand2")
    upload_btn.pack(fill="x", padx=25, pady=10)
    
    root.mainloop()

# CLI Mode
def start_cli(file_path, mode):
    try:
        print(f"🔄 CSV 파일 읽는 중: {file_path}...")
        members = parse_csv(file_path)
        total_count = len(members)
        if total_count == 0:
            print("⚠️ 업로드할 유효한 데이터가 없습니다.")
            return
            
        print(f"📊 파싱 및 정규화 완료: 총 {total_count}명의 데이터가 로드되었습니다.")
        print("💡 [정제 데이터 샘플 (최대 3명)]")
        for i, m in enumerate(members[:3]):
            print(f"  [{i+1}] 이름: {m['이름']} | 부수: {m['부수']} | 라켓: {m['라켓']}")
            
        print(f"\n⚙️ 설정된 작업 모드: {mode.upper()}")
        confirm = input("⚠️ 실제로 구글 시트('탁우회_명단')에 업로드하시겠습니까? (y/n): ").strip().lower()
        if confirm != 'y':
            print("❌ 업로드 작업을 취소했습니다.")
            return
            
        print("🔑 구글 API 인증 및 시트 연결 중...")
        count, error = run_upload(file_path, mode)
        if error:
            print(f"❌ 오류: {error}")
        else:
            print(f"🎉 성공적으로 업로드를 완료했습니다! (반영된 부원: {count}명)")
            
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="탁우회 구글 시트 회원 데이터 일괄 업로드 유틸리티 (GUI/CLI 듀얼 지원)")
    parser.add_argument("file", nargs="?", help="업로드할 CSV 파일 경로 (생략 시 GUI 모드 실행)")
    parser.add_argument("--mode", choices=["overwrite", "append"], default="overwrite",
                        help="업로드 모드: overwrite(기존 데이터 덮어쓰기, 기본값) 또는 append(기존 데이터 뒤에 추가)")
    args = parser.parse_args()
    
    # If no file is specified, start GUI. Otherwise, run in CLI mode.
    if args.file is None:
        start_gui()
    else:
        start_cli(args.file, args.mode)

if __name__ == "__main__":
    main()
