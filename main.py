import os
import math
import threading
import queue
import subprocess
import tempfile
import json
import socket
import platform
import struct
import datetime
import customtkinter as ctk
import numpy as np
import yt_dlp
from tkinter import filedialog, messagebox

# Configuración de apariencia
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

APP_VERSION = "3.0 (GPU Focus)"

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
            self.error("FFmpeg no encontrado. Asegúrate de que está instalado y en el PATH.")
            return False

    def get_system_info(self):
        return f"{socket.gethostname()} ({platform.system()})"

    # --- CODIFICACIÓN (ARCHIVO -> VIDEO) ---
    def file_to_video_stream(self, input_path, output_path, width=1920, height=1080, pixel_size=4, fps=24, encoder="CPU (libx264)"):
        if not self.check_ffmpeg():
            self.finished()
            return

        file_size = os.path.getsize(input_path)
        filename = os.path.basename(input_path)
        
        # 1. Crear Cabecera Inteligente (Smart Header)
        header_data = {
            "filename": filename,
            "original_size": file_size,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "created_by": self.get_system_info(),
            "version": APP_VERSION
        }
        header_json = json.dumps(header_data).encode('utf-8')
        
        # Estructura: [MAGIC "ISG2" (4 bytes)] + [Largo Header (4 bytes)] + [JSON Bytes]
        magic_bytes = b'ISG2'
        length_bytes = struct.pack('>I', len(header_json))
        full_header = magic_bytes + length_bytes + header_json
        
        self.log(f"Procesando: {filename}")
        self.log(f"Usando: {encoder}")
        self.log(f"Metadatos incrustados: {header_data}")

        total_data_size = len(full_header) + file_size
        
        cols = width // pixel_size
        rows = height // pixel_size
        bits_per_frame = cols * rows
        bytes_per_frame = bits_per_frame // 8
        total_frames = math.ceil((total_data_size * 8) / bits_per_frame)
        
        # Configurar Encoder (GPU vs CPU)
        codec_args = []
        if "NVIDIA" in encoder:
            codec_args = ['-c:v', 'h264_nvenc', '-rc', 'constqp', '-qp', '0', '-preset', 'p1']
        elif "AMD" in encoder:
            codec_args = ['-c:v', 'h264_amf', '-rc', 'cqp', '-qp_i', '0', '-qp_p', '0', '-quality', 'speed']
        elif "Intel" in encoder:
            codec_args = ['-c:v', 'h264_qsv', '-global_quality', '0', '-look_ahead', '0']
        else: 
            codec_args = ['-c:v', 'libx264', '-crf', '0', '-preset', 'ultrafast']

        command = [
            'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo',
            '-s', f'{width}x{height}', '-pix_fmt', 'gray', 
            '-r', str(fps), '-i', '-', 
        ] + codec_args + [
            '-pix_fmt', 'yuv420p', '-threads', '4', output_path
        ]
        
        log_file = tempfile.TemporaryFile()

        try:
            # Buffer grande (10MB) para velocidad
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log_file, bufsize=10**7)
            
            # A. Escribir Cabecera
            process.stdin.write(self.bits_to_frame_bytes(full_header, cols, rows, pixel_size))
            
            # B. Escribir Archivo
            with open(input_path, "rb") as f:
                frame_idx = 0
                while True:
                    if process.poll() is not None: break
                    if self.stop_event.is_set(): process.terminate(); return

                    chunk = f.read(bytes_per_frame)
                    if not chunk: break
                    
                    frame_data = self.bits_to_frame_bytes(chunk, cols, rows, pixel_size)
                    try: process.stdin.write(frame_data)
                    except: break

                    frame_idx += 1
                    if frame_idx % 60 == 0:
                        self.progress(frame_idx / total_frames, f"Codificando: {frame_idx}/{total_frames}")

            process.stdin.close()
            process.wait()
            
            if process.returncode == 0:
                self.success(f"Video guardado correctamente:\n{output_path}")
            else:
                log_file.seek(0)
                self.error(f"Error FFMPEG: {log_file.read().decode(errors='ignore')}")

        except Exception as e:
            self.error(f"Error crítico: {e}")
        finally:
            log_file.close()
            self.finished()

    def bits_to_frame_bytes(self, data_bytes, cols, rows, pixel_size):
        # Función auxiliar optimizada para convertir bytes -> imagen raw
        bits = np.unpackbits(np.frombuffer(data_bytes, dtype=np.uint8))
        if len(bits) < cols * rows:
            bits = np.pad(bits, (0, (cols * rows) - len(bits)), 'constant')
        grid = bits.reshape((rows, cols))
        frame_gray = grid.repeat(pixel_size, axis=0).repeat(pixel_size, axis=1)
        # Invertir colores (0=Blanco, 1=Negro) para mejor compresión
        return ((1 - frame_gray) * 255).astype(np.uint8).tobytes()

    # --- DECODIFICACIÓN (VIDEO -> ARCHIVO) ---
    def video_to_file_stream(self, input_path, output_folder):
        if not self.check_ffmpeg():
            self.finished()
            return

        self.log(f"Analizando: {os.path.basename(input_path)}")

        try:
            probe = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,nb_frames', '-of', 'csv=p=0', input_path]).decode().strip().split(',')
            width = int(probe[0])
            height = int(probe[1])
            try: total_frames = int(probe[2]) 
            except: total_frames = 1000
        except:
            self.error("No se pudo leer el video.")
            self.finished()
            return

        pixel_size = 4
        command = ['ffmpeg', '-hwaccel', 'auto', '-i', input_path, '-f', 'image2pipe', '-pix_fmt', 'gray', '-vcodec', 'rawvideo', '-']

        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**7)
            frame_size = width * height
            offset = pixel_size // 2
            
            byte_stream = bytearray()
            
            # --- FASE 1: LEER CABECERA (PRIMER FRAME) ---
            raw_frame = process.stdout.read(frame_size)
            if not raw_frame:
                self.error("Video vacío")
                return

            # Decodificar primer frame
            frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((height, width))
            sampled = frame[offset::pixel_size, offset::pixel_size]
            bits = (sampled < 128).astype(np.uint8)
            decoded_bytes = np.packbits(bits).tobytes()
            byte_stream.extend(decoded_bytes)

            # Detectar firma ISG2
            magic = byte_stream[:4]
            if magic != b'ISG2':
                self.log("Aviso: Video antiguo o sin cabecera Smart. Recuperando modo RAW.")
                target_filename = "recuperado_raw.bin"
                target_size = None
                header_offset = 0
            else:
                header_len = struct.unpack('>I', byte_stream[4:8])[0]
                json_bytes = byte_stream[8:8+header_len]
                try:
                    metadata = json.loads(json_bytes.decode('utf-8'))
                    self.message_queue.put(("metadata_found", metadata)) # Preguntar al usuario
                    
                    target_filename = metadata.get('filename', 'recuperado.bin')
                    target_size = metadata.get('original_size', None)
                    header_offset = 8 + header_len
                except Exception as e:
                    self.error(f"Cabecera corrupta: {e}")
                    return

            final_output_path = os.path.join(output_folder, target_filename)
            current_written = 0
            
            # --- FASE 2: GUARDAR ARCHIVO ---
            with open(final_output_path, "wb") as f:
                # Escribir lo que sobró del primer frame
                chunk = byte_stream[header_offset:]
                f.write(chunk)
                current_written += len(chunk)
                
                frame_idx = 1
                while True:
                    if self.stop_event.is_set(): process.terminate(); self.finished(); return

                    raw_frame = process.stdout.read(frame_size)
                    if not raw_frame: break
                    
                    frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((height, width))
                    sampled = frame[offset::pixel_size, offset::pixel_size]
                    bits = (sampled < 128).astype(np.uint8)
                    chunk = np.packbits(bits).tobytes()
                    
                    # Cortar exacto si tenemos el tamaño original
                    if target_size and (current_written + len(chunk) > target_size):
                        remaining = target_size - current_written
                        f.write(chunk[:remaining])
                        break 
                    else:
                        f.write(chunk)
                        current_written += len(chunk)
                    
                    frame_idx += 1
                    if frame_idx % 100 == 0:
                        prog = min(frame_idx / total_frames, 0.99)
                        self.progress(prog, f"Recuperando: {frame_idx}")

            process.wait()
            self.progress(1.0, "Completado")
            self.success(f"Recuperado exitosamente:\n{target_filename}")

        except Exception as e:
            self.error(f"Error: {e}")
        finally:
            self.finished()

    # --- YOUTUBE ---
    def download_youtube(self, url, output_folder):
        self.log(f"Iniciando descarga: {url}")
        def progress_hook(d):
            if d['status'] == 'downloading':
                try:
                    p = d.get('_percent_str', '0%').replace('%','')
                    self.progress(float(p)/100, f"Descargando: {d.get('_percent_str')}")
                except: pass
        
        ydl_opts = {
            'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
            'outtmpl': os.path.join(output_folder, '%(title)s.%(ext)s'),
            'progress_hooks': [progress_hook],
            'noplaylist': True, 'quiet': True, 'no_warnings': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                self.success(f"Descargado: {info['title']}")
        except Exception as e:
            self.error(f"Error YouTube: {e}")
        finally:
            self.finished()

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Infinite Storage Glitch - GPU Edition")
        self.geometry("900x700")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.message_queue = queue.Queue()
        self.logic = StorageGlitch(message_queue=self.message_queue)

        # UI Layout
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.main_frame.grid_columnconfigure(0, weight=1)
        
        self.tab_view = ctk.CTkTabview(self.main_frame)
        self.tab_view.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        
        self.tab1 = self.tab_view.add("Codificar")
        self.tab2 = self.tab_view.add("Decodificar")
        self.tab3 = self.tab_view.add("YouTube")

        self.setup_ui()
        self.after(100, self.check_queue)

    def setup_ui(self):
        # --- TAB 1: CODIFICAR ---
        t1 = self.tab1
        t1.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(t1, text="Archivo a Video", font=("Arial", 16, "bold")).grid(pady=10)
        
        ctk.CTkButton(t1, text="Seleccionar Archivo", command=self.sel_file).grid(pady=10)
        self.lbl_file = ctk.CTkLabel(t1, text="...", text_color="gray")
        self.lbl_file.grid(pady=5)
        
        # Solo selector de GPU, sin hilos
        perf_frame = ctk.CTkFrame(t1)
        perf_frame.grid(pady=20)
        ctk.CTkLabel(perf_frame, text="Aceleración de Hardware:").grid(row=0, column=0, padx=10)
        self.option_encoder = ctk.CTkOptionMenu(perf_frame, values=["CPU (libx264)", "NVIDIA (h264_nvenc)", "AMD (h264_amf)", "Intel (h264_qsv)"])
        self.option_encoder.grid(row=0, column=1, padx=10, pady=10)

        self.btn_enc = ctk.CTkButton(t1, text="Generar Video", command=self.run_enc, state="disabled", fg_color="green")
        self.btn_enc.grid(pady=20)

        # --- TAB 2: DECODIFICAR ---
        t2 = self.tab2
        t2.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(t2, text="Video a Archivo", font=("Arial", 16, "bold")).grid(pady=10)
        ctk.CTkButton(t2, text="Seleccionar Video", command=self.sel_vid).grid(pady=10)
        self.lbl_vid = ctk.CTkLabel(t2, text="...", text_color="gray")
        self.lbl_vid.grid(pady=5)
        
        self.out_folder = os.getcwd()
        ctk.CTkLabel(t2, text=f"Salida: {self.out_folder}").grid(pady=5)
        ctk.CTkButton(t2, text="Cambiar Carpeta", command=self.sel_folder).grid(pady=5)
        
        self.btn_dec = ctk.CTkButton(t2, text="Analizar y Recuperar", command=self.run_dec, state="disabled", fg_color="green")
        self.btn_dec.grid(pady=20)

        # --- TAB 3: YOUTUBE ---
        t3 = self.tab3
        t3.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(t3, text="YouTube URL:").grid(pady=10)
        self.entry_url = ctk.CTkEntry(t3, width=400)
        self.entry_url.grid(pady=10)
        ctk.CTkButton(t3, text="Descargar", command=self.run_yt, fg_color="red").grid(pady=20)

        # --- STATUS BAR ---
        self.status = ctk.CTkFrame(self.main_frame)
        self.status.grid(row=1, sticky="ew", padx=10, pady=10)
        self.status.grid_columnconfigure(0, weight=1)
        self.pbar = ctk.CTkProgressBar(self.status)
        self.pbar.grid(sticky="ew", padx=10, pady=10)
        self.pbar.set(0)
        self.lbl_stat = ctk.CTkLabel(self.status, text="Listo")
        self.lbl_stat.grid(sticky="w", padx=10)
        self.log = ctk.CTkTextbox(self.status, height=100)
        self.log.grid(sticky="ew", padx=10, pady=10)

    # Funciones UI
    def sel_file(self):
        f = filedialog.askopenfilename()
        if f: 
            self.file_path = f
            self.lbl_file.configure(text=os.path.basename(f), text_color="white")
            self.btn_enc.configure(state="normal")

    def run_enc(self):
        out = filedialog.asksaveasfilename(defaultextension=".mp4")
        if out:
            self.btn_enc.configure(state="disabled")
            # Hilos fijo en 4 (valor seguro), Encoder viene del menú
            threading.Thread(target=self.logic.file_to_video_stream, args=(self.file_path, out, 1920, 1080, 4, 24, self.option_encoder.get())).start()

    def sel_vid(self):
        f = filedialog.askopenfilename()
        if f:
            self.vid_path = f
            self.lbl_vid.configure(text=os.path.basename(f), text_color="white")
            self.btn_dec.configure(state="normal")
    
    def sel_folder(self):
        d = filedialog.askdirectory()
        if d: self.out_folder = d

    def run_dec(self):
        self.btn_dec.configure(state="disabled")
        threading.Thread(target=self.logic.video_to_file_stream, args=(self.vid_path, self.out_folder)).start()

    def run_yt(self):
        url = self.entry_url.get()
        if url: threading.Thread(target=self.logic.download_youtube, args=(url, self.out_folder)).start()

    def check_queue(self):
        try:
            while True:
                msg, content = self.message_queue.get_nowait()
                if msg == "log": self.log.insert("end", content+"\n"); self.log.see("end")
                elif msg == "progress": self.pbar.set(content[0]); self.lbl_stat.configure(text=content[1])
                elif msg == "success": messagebox.showinfo("Éxito", content); self.pbar.set(1.0); self.lbl_stat.configure(text="Listo")
                elif msg == "error": messagebox.showerror("Error", content)
                elif msg == "finished": 
                    self.btn_enc.configure(state="normal"); self.btn_dec.configure(state="normal")
                
                # --- POPUP DE METADATOS ---
                elif msg == "metadata_found":
                    meta = content
                    info_text = (
                        f"¡ARCHIVO DETECTADO!\n\n"
                        f"📄 Nombre: {meta['filename']}\n"
                        f"💾 Tamaño: {meta['original_size']} bytes\n"
                        f"📅 Fecha: {meta['created_at']}\n"
                        f"💻 PC: {meta['created_by']}\n"
                        f"⚙️ Versión: {meta['version']}\n\n"
                        f"¿Deseas recuperarlo?"
                    )
                    if not messagebox.askyesno("Metadatos Encontrados", info_text):
                        self.logic.stop_event.set()

        except queue.Empty: pass
        finally: self.after(100, self.check_queue)

if __name__ == "__main__":
    app = App()
    app.mainloop()