import os
import math
import threading
import queue
import subprocess
import json
import socket
import platform
import struct
import shutil
import customtkinter as ctk
import numpy as np
import yt_dlp
from tkinter import filedialog, messagebox

# Configuración de apariencia
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class StorageGlitch:
    def __init__(self, message_queue):
        self.message_queue = message_queue
        self.stop_event = threading.Event()

    def log(self, message):
        self.message_queue.put(("log", message))

    def progress(self, value, message=None):
        self.message_queue.put(("progress", (value, message)))

    def success(self, message):
        self.message_queue.put(("success", message))
    
    def error(self, message):
        self.message_queue.put(("error", message))
        
    def finished(self):
        self.message_queue.put(("finished", None))

    def check_ffmpeg(self):
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except FileNotFoundError:
            self.error("FFmpeg no encontrado. Instálalo y agrégalo al PATH.")
            return False

    # --- CODIFICACIÓN ---
    def file_to_video_stream(self, input_path, output_path, width=1920, height=1080, pixel_size=4, fps=24, encoder="CPU (libx264)"):
        if not self.check_ffmpeg(): self.finished(); return

        file_size = os.path.getsize(input_path)
        filename = os.path.basename(input_path)
        
        header = json.dumps({"filename": filename, "size": file_size}).encode('utf-8')
        full_header = b'ISG2' + struct.pack('>I', len(header)) + header
        
        self.log(f"Codificando: {filename}")

        cols = width // pixel_size
        rows = height // pixel_size
        bytes_per_frame = (cols * rows) // 8
        
        total_size = len(full_header) + file_size
        total_frames = math.ceil((total_size * 8) / (cols * rows))

        codec_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '0']
        if "NVIDIA" in encoder: codec_args = ['-c:v', 'h264_nvenc', '-preset', 'p1']
        elif "AMD" in encoder: codec_args = ['-c:v', 'h264_amf']
        elif "Intel" in encoder: codec_args = ['-c:v', 'h264_qsv']

        command = [
            'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo',
            '-s', f'{width}x{height}', '-pix_fmt', 'gray', '-r', str(fps),
            '-i', '-'
        ] + codec_args + ['-pix_fmt', 'yuv420p', output_path]

        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**7)
            buffer = bytearray(full_header)
            
            with open(input_path, "rb") as f:
                frame_idx = 0
                while True:
                    if self.stop_event.is_set(): process.terminate(); return

                    while len(buffer) < bytes_per_frame:
                        chunk = f.read(1024*1024)
                        if not chunk: break
                        buffer.extend(chunk)
                    
                    if len(buffer) == 0: break 
                    
                    while len(buffer) >= bytes_per_frame:
                        frame_bytes = buffer[:bytes_per_frame]
                        buffer = buffer[bytes_per_frame:]
                        
                        bits = np.unpackbits(np.frombuffer(frame_bytes, dtype=np.uint8))
                        grid = bits.reshape((rows, cols))
                        frame = grid.repeat(pixel_size, axis=0).repeat(pixel_size, axis=1)
                        frame_bytes_out = ((1 - frame) * 255).astype(np.uint8).tobytes()
                        
                        try: process.stdin.write(frame_bytes_out)
                        except: break
                        
                        frame_idx += 1
                        if frame_idx % 50 == 0: self.progress(frame_idx/total_frames, f"Frame {frame_idx}/{total_frames}")

                    if len(buffer) > 0 and f.tell() == file_size:
                        bits = np.unpackbits(np.frombuffer(buffer, dtype=np.uint8))
                        if len(bits) < (cols*rows):
                            bits = np.pad(bits, (0, (cols*rows)-len(bits)), 'constant')
                        grid = bits.reshape((rows, cols))
                        frame = grid.repeat(pixel_size, axis=0).repeat(pixel_size, axis=1)
                        frame_bytes_out = ((1 - frame) * 255).astype(np.uint8).tobytes()
                        process.stdin.write(frame_bytes_out)
                        buffer = bytearray()
                        break

            process.stdin.close()
            process.wait()
            self.success(f"Video creado:\n{output_path}")

        except Exception as e:
            self.error(f"Error: {e}")
        finally:
            self.finished()

    # --- DECODIFICACIÓN ---
    def video_to_file_stream(self, input_path, output_folder):
        if not self.check_ffmpeg(): self.finished(); return

        self.log(f"Recuperando: {os.path.basename(input_path)}")

        try:
            # Obtener dimensiones
            probe = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', input_path]).decode().strip().split(',')
            width = int(probe[0])
            height = int(probe[1])
        except: self.error("Error leyendo video"); self.finished(); return

        pixel_size = 4
        offset = pixel_size // 2
        
        # --- OPTIMIZACIÓN: BATCHING ---
        # Procesaremos 60 frames de golpe para acelerar Numpy
        BATCH_FRAMES = 60 
        frame_bytes = width * height
        batch_bytes = frame_bytes * BATCH_FRAMES
        
        temp_bin = os.path.join(output_folder, "temp_raw.bin")

        command = [
            'ffmpeg', '-y', '-i', input_path, 
            '-f', 'image2pipe', '-pix_fmt', 'gray', '-vcodec', 'rawvideo', '-'
        ]

        try:
            # Aumentamos el buffer del pipe
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**8)
            
            with open(temp_bin, "wb") as f_out:
                while True:
                    if self.stop_event.is_set(): process.terminate(); return
                    
                    # Leemos un bloque grande de datos (varios frames)
                    raw_batch = process.stdout.read(batch_bytes)
                    if not raw_batch: break
                    
                    read_len = len(raw_batch)
                    
                    # Si el bloque está incompleto (final del video), rellenamos con ceros
                    if read_len % frame_bytes != 0:
                        missing = frame_bytes - (read_len % frame_bytes)
                        raw_batch += b'\x00' * missing
                    
                    # Calcular cuántos frames tenemos en este lote
                    num_frames = len(raw_batch) // frame_bytes
                    
                    # --- MAGIA DE NUMPY (Vectorización) ---
                    # 1. Convertimos todo el bloque a un array 3D (frames, height, width)
                    batch_np = np.frombuffer(raw_batch, dtype=np.uint8).reshape((num_frames, height, width))
                    
                    # 2. Hacemos el slicing (recorte) en los 3 ejes a la vez
                    # [:, y, x] -> ":" selecciona todos los frames del lote
                    sampled = batch_np[:, offset::pixel_size, offset::pixel_size]
                    
                    # 3. Umbralización y empaquetado de bits masivo
                    bits = (sampled < 128).astype(np.uint8)
                    bytes_out = np.packbits(bits).tobytes()
                    
                    f_out.write(bytes_out)

            process.wait()
            
            # --- FASE DE RECUPERACIÓN DE CABECERA (Igual que antes) ---
            if os.path.exists(temp_bin):
                self.log("Procesando archivo final...") # Feedback al usuario
                with open(temp_bin, "rb") as f:
                    magic = f.read(4)
                    if magic == b'ISG2':
                        try:
                            hlen_bytes = f.read(4)
                            if not hlen_bytes: raise Exception("Archivo corrupto o vacío")
                            hlen = struct.unpack('>I', hlen_bytes)[0]
                            header_data = f.read(hlen)
                            header = json.loads(header_data.decode('utf-8'))
                            
                            real_name = header['filename']
                            real_size = header['size']
                            
                            self.log(f"Archivo detectado: {real_name}")
                            final_path = os.path.join(output_folder, real_name)
                            
                            # Optimización de escritura final: Copia por bloques grandes
                            with open(final_path, "wb") as f_final:
                                while True:
                                    chunk = f.read(1024*1024*5) # 5MB chunks
                                    if not chunk: break
                                    f_final.write(chunk)
                            
                            # Truncar al tamaño exacto
                            with open(final_path, "a+b") as f_final:
                                f_final.truncate(real_size)
                                
                            self.success(f"Recuperado: {real_name}")
                        except Exception as e:
                            self.error(f"Error de cabecera: {e}")
                    else:
                        final_path = os.path.join(output_folder, "recuperado_raw.bin")
                        shutil.copy(temp_bin, final_path)
                        self.success("Recuperado sin cabecera (RAW)")
                
                try: os.remove(temp_bin)
                except: pass

        except Exception as e:
            self.error(f"Error: {e}")
            print(e) # Para debug en consola si es necesario
        finally:
            self.finished()

    # --- YOUTUBE ---
    def download_youtube(self, url, output_folder):
        self.log("Iniciando descarga de YouTube...")
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': os.path.join(output_folder, '%(title)s.%(ext)s'),
            'noplaylist': True, 'quiet': True
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
            self.success("Descarga completada")
        except Exception as e: self.error(str(e))
        finally: self.finished()

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ISG - Infinite Storage Glitch")
        self.geometry("900x650")
        
        # Configuración de grid principal
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1) # Tabview expandible
        self.grid_rowconfigure(2, weight=0) # Log estático

        self.queue = queue.Queue()
        self.logic = StorageGlitch(self.queue)
        
        # --- TABVIEW PRINCIPAL ---
        self.tab = ctk.CTkTabview(self)
        self.tab.grid(row=0, column=0, padx=20, pady=(10, 0), sticky="nsew")
        
        t1 = self.tab.add("Codificar")
        t2 = self.tab.add("Decodificar")
        t3 = self.tab.add("YouTube")
        
        # ================= TAB 1: CODIFICAR =================
        self.f1_input = ctk.CTkFrame(t1)
        self.f1_input.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(self.f1_input, text="Archivo de Entrada:", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(10,0))
        
        self.file_entry = ctk.CTkEntry(self.f1_input, placeholder_text="Ningún archivo seleccionado...")
        self.file_entry.pack(side="left", fill="x", expand=True, padx=10, pady=10)
        
        ctk.CTkButton(self.f1_input, text="Examinar", width=100, command=self.sel_file).pack(side="right", padx=10, pady=10)

        self.f1_config = ctk.CTkFrame(t1)
        self.f1_config.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(self.f1_config, text="Encoder:", font=("Arial", 12)).pack(side="left", padx=10, pady=10)
        self.enc_opt = ctk.CTkOptionMenu(self.f1_config, values=["CPU (libx264)", "NVIDIA", "AMD", "Intel"])
        self.enc_opt.pack(side="left", padx=10, pady=10)

        self.btn_enc = ctk.CTkButton(t1, text="INICIAR CODIFICACIÓN", height=50, font=("Arial", 14, "bold"), 
                                     fg_color="#2CC985", hover_color="#229A65", text_color="white", command=self.run_enc)
        self.btn_enc.pack(fill="x", padx=10, pady=20)

        # ================= TAB 2: DECODIFICAR =================
        self.f2_input = ctk.CTkFrame(t2)
        self.f2_input.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(self.f2_input, text="Video Glitch:", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(10,0))
        self.vid_entry = ctk.CTkEntry(self.f2_input, placeholder_text="Selecciona el video...")
        self.vid_entry.pack(fill="x", padx=10, pady=(5,10))
        ctk.CTkButton(self.f2_input, text="Buscar Video", command=self.sel_vid).pack(anchor="e", padx=10, pady=(0,10))

        self.btn_fold = ctk.CTkButton(t2, text="📂 Seleccionar Carpeta de Salida", fg_color="transparent", border_width=2, text_color=("gray10", "#DCE4EE"), command=self.sel_fold)
        self.btn_fold.pack(fill="x", padx=10, pady=5)
        
        self.btn_dec = ctk.CTkButton(t2, text="RECUPERAR ARCHIVOS", height=50, font=("Arial", 14, "bold"),
                                     fg_color="#3B8ED0", hover_color="#36719F", command=self.run_dec)
        self.btn_dec.pack(fill="x", padx=10, pady=20)

        # ================= TAB 3: YOUTUBE =================
        self.f3_input = ctk.CTkFrame(t3)
        self.f3_input.pack(fill="both", expand=True, padx=20, pady=20)
        
        ctk.CTkLabel(self.f3_input, text="URL del Video:", font=("Arial", 14)).pack(pady=(40,10))
        self.url = ctk.CTkEntry(self.f3_input, width=400, placeholder_text="https://youtube.com/watch?v=...")
        self.url.pack(pady=10)
        
        # BOTÓN ROJO PARA YOUTUBE
        ctk.CTkButton(self.f3_input, text="DESCARGAR VIDEO", height=40, width=200, font=("Arial", 12, "bold"),
                      fg_color="#CC0000", hover_color="#990000",
                      command=self.run_yt).pack(pady=20)

        # ================= BARRA DE PROGRESO Y LOGS =================
        self.lbl_status = ctk.CTkLabel(self, text="Listo", text_color="gray")
        self.lbl_status.grid(row=1, column=0, sticky="w", padx=25)
        
        self.p_bar = ctk.CTkProgressBar(self)
        self.p_bar.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")
        self.p_bar.set(0)

        self.log = ctk.CTkTextbox(self, height=120, font=("Consolas", 12), fg_color="#1a1a1a", text_color="#00ff00")
        self.log.grid(row=3, column=0, padx=20, pady=20, sticky="ew")
        
        self.out_fold = os.getcwd()
        self.after(100, self.chk_q)

    def sel_file(self):
        f = filedialog.askopenfilename()
        if f: 
            self.f_path = f
            self.file_entry.delete(0, "end")
            self.file_entry.insert(0, os.path.basename(f))
    
    def run_enc(self):
        if not hasattr(self, 'f_path'): messagebox.showerror("Error", "Selecciona un archivo primero"); return
        out = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4 Video", "*.mp4")])
        if out: 
            self.disable_ui(True)
            self.p_bar.set(0)
            threading.Thread(target=self.logic.file_to_video_stream, args=(self.f_path, out, 1920, 1080, 4, 24, self.enc_opt.get())).start()

    def sel_vid(self):
        f = filedialog.askopenfilename(filetypes=[("Video files", "*.mp4 *.avi *.mkv")])
        if f: 
            self.v_path = f
            self.vid_entry.delete(0, "end")
            self.vid_entry.insert(0, os.path.basename(f))
        
    def sel_fold(self): 
        d = filedialog.askdirectory()
        if d: 
            self.out_fold = d
            self.btn_fold.configure(text=f"📂 Salida: {os.path.basename(d)}")
        
    def run_dec(self): 
        if not hasattr(self, 'v_path'): messagebox.showerror("Error", "Selecciona un video"); return
        self.disable_ui(True)
        self.p_bar.set(0)
        self.p_bar.start() # Modo indeterminado
        threading.Thread(target=self.logic.video_to_file_stream, args=(self.v_path, self.out_fold)).start()

    def run_yt(self): 
        if not self.url.get(): return
        self.disable_ui(True)
        self.p_bar.start()
        threading.Thread(target=self.logic.download_youtube, args=(self.url.get(), self.out_fold)).start()

    def disable_ui(self, disabled):
        state = "disabled" if disabled else "normal"
        self.btn_enc.configure(state=state)
        self.btn_dec.configure(state=state)

    def chk_q(self):
        try:
            while True:
                type_, data = self.queue.get_nowait()
                
                if type_ == "log": 
                    self.log.insert("end", f"> {data}\n")
                    self.log.see("end")
                
                elif type_ == "progress":
                    val, msg = data
                    self.p_bar.stop()
                    self.p_bar.set(val)
                    if msg: self.lbl_status.configure(text=msg)

                elif type_ == "success": 
                    messagebox.showinfo("Éxito", data)
                    self.p_bar.set(1)
                    self.lbl_status.configure(text="Completado")
                    self.disable_ui(False)

                elif type_ == "error": 
                    messagebox.showerror("Error", data)
                    self.p_bar.set(0)
                    self.lbl_status.configure(text="Error")
                    self.disable_ui(False)
                    
                elif type_ == "finished":
                    self.p_bar.stop()
                    self.disable_ui(False)
                    self.lbl_status.configure(text="Listo")

        except queue.Empty: pass
        self.after(100, self.chk_q)

if __name__ == "__main__": App().mainloop()