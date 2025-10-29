import sys
import os
import subprocess
import tempfile
import glob # 2-pass 로그 파일 삭제를 위해 import
from abc import ABC, abstractmethod

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QFileDialog, QTextEdit, QMessageBox,
    QLabel, QFrame, QTabWidget, QLineEdit, QComboBox, QGridLayout,
    QSizePolicy, QSpacerItem
)
from PySide6.QtCore import QThread, Signal, Qt

# --- [Part 1] 전략 (Jobs) 정의 (build_commands로 변경) ---

class BaseMediaJob(ABC):
    """(v6.0) 모든 FFmpeg 작업 명세서의 '규칙'(인터페이스)"""
    def __init__(self, output_file):
        self.output_file = output_file
        self.temp_file_name = None 
        # 2-pass 로그 파일의 기본 이름 (job에서 변경 가능)
        self.pass_log_prefix = "ffmpeg2pass-log" 

    @abstractmethod
    def build_commands(self) -> list[list[str]]:
        """
        [v6.0] 자식 클래스가 반드시 구현해야 할 '명령어 생성' 메서드
        반환값: 리스트의 리스트 (순차 실행될 명령어 목록)
        (예: 1-pass의 경우 [ [cmd] ], 2-pass의 경우 [ [pass1_cmd], [pass2_cmd] ])
        """
        pass
    
    def cleanup(self):
        """작업 완료 후 임시 파일 등을 정리하는 메서드"""
        cleaned_messages = []
        if self.temp_file_name and os.path.exists(self.temp_file_name):
            os.remove(self.temp_file_name)
            cleaned_messages.append(f"임시 파일 삭제: {self.temp_file_name}")
            
        # [v6.0] 2-pass 로그 파일들 (ffmpeg2pass-log.log, .mbtree 등) 삭제
        log_files = glob.glob(f"{self.pass_log_prefix}*")
        if log_files:
            for log_file in log_files:
                try:
                    os.remove(log_file)
                    cleaned_messages.append(f"로그 파일 삭제: {log_file}")
                except OSError:
                    pass # 파일이 사용 중이어도 무시
                    
        return "\n".join(cleaned_messages) if cleaned_messages else None

class MergeDemuxerJob(BaseMediaJob):
    """'빠른 병합' 전략"""
    def __init__(self, file_list, output_file):
        super().__init__(output_file)
        self.file_list = file_list

    def build_commands(self) -> list[list[str]]: # [v6.0] 이름 및 반환 타입 변경
        # 1. 임시 파일 생성
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as temp_list_file:
            for file_path in self.file_list:
                safe_path = file_path.replace(os.sep, '/')
                temp_list_file.write(f"file '{safe_path}'\n")
            self.temp_file_name = temp_list_file.name
        
        # 2. 명령어 생성
        command = [
            'ffmpeg', '-f', 'concat', '-safe', '0',
            '-i', self.temp_file_name,
            '-c', 'copy', '-y', self.output_file
        ]
        return [command] # [v6.0] 1-pass이므로 리스트로 한 번 감싸서 반환

class MergeFilterJob(BaseMediaJob):
    """'재인코딩 병합' 전략 (v4.0 로직)"""
    def __init__(self, file_list, output_file):
        super().__init__(output_file)
        self.file_list = file_list

    def build_commands(self) -> list[list[str]]: # [v6.0] 이름 및 반환 타입 변경
        # ... (v5.0과 동일한 명령어 생성 로직) ...
        TARGET_WIDTH = 1920
        TARGET_HEIGHT = 1080
        TARGET_SAR = "1" 
        TARGET_AUDIO_RATE = "44100"
        TARGET_AUDIO_LAYOUT = "stereo"
        input_args, filter_parts, concat_inputs = [], [], ""
        file_count = len(self.file_list)
        for i in range(file_count):
            input_args.extend(['-i', self.file_list[i]])
            video_filter = (f"[{i}:v]scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
                            f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar={TARGET_SAR}[v{i}]")
            filter_parts.append(video_filter)
            audio_filter = (f"[{i}:a]aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts={TARGET_AUDIO_LAYOUT}[a{i}]")
            filter_parts.append(audio_filter)
            concat_inputs += f"[v{i}][a{i}]" 
        concat_filter = f"{concat_inputs}concat=n={file_count}:v=1:a=1[v][a]"
        filter_parts.append(concat_filter)
        filter_string = ";".join(filter_parts)
        command = ['ffmpeg', *input_args,
                   '-filter_complex', filter_string,
                   '-map', '[v]', '-map', '[a]', '-y',
                   '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
                   '-c:a', 'aac', '-b:a', '128k',
                   self.output_file]
        return [command] # [v6.0] 1-pass이므로 리스트로 한 번 감싸서 반환

class RecodeJob(BaseMediaJob):
    """[v6.0] '재인코딩' 전략 (CRF 및 2-Pass VBR 지원)"""
    def __init__(self, input_file, output_file, 
                 v_codec, v_mode, v_crf, v_preset, v_bitrate,
                 a_codec, a_mode, a_bitrate, a_qscale, 
                 scale):
        super().__init__(output_file)
        self.input_file = input_file
        
        # 비디오 옵션
        self.v_codec = v_codec
        self.v_mode = v_mode     # 'CRF' or '2-Pass'
        self.v_crf = v_crf
        self.v_preset = v_preset
        self.v_bitrate = v_bitrate
        
        # 오디오 옵션
        self.a_codec = a_codec
        self.a_mode = a_mode     # 'ABR' or 'VBR'
        self.a_bitrate = a_bitrate
        self.a_qscale = a_qscale # (LAME -q:a)
        
        # 필터 옵션
        self.scale = scale.strip()

    def build_commands(self) -> list[list[str]]:
        """[v6.0] GUI 옵션에 따라 1-pass(CRF) 또는 2-pass 명령어를 생성"""
        
        # --- 공통 옵션 구성 ---
        base_cmd = ['ffmpeg', '-y', '-i', self.input_file]
        video_opts, audio_opts, filter_opts = [], [], []
        commands_to_run = []

        # 1. 오디오 옵션 구성
        if self.a_codec == 'copy':
            audio_opts = ['-c:a', 'copy']
        elif self.a_codec == 'libmp3lame':
            audio_opts = ['-c:a', 'libmp3lame']
            if self.a_mode == 'VBR':
                audio_opts.extend(['-q:a', self.a_qscale]) # MP3 VBR (품질)
            else: # 'ABR'
                audio_opts.extend(['-b:a', self.a_bitrate]) # MP3 ABR (비트레이트)
        else: # 'aac' 등
            audio_opts = ['-c:a', self.a_codec]
            if self.a_bitrate:
                audio_opts.extend(['-b:a', self.a_bitrate]) # AAC ABR (비트레이트)

        # 2. 비디오 필터 (해상도) 구성
        if self.scale:
            filter_opts = ['-vf', f"scale={self.scale}"]

        # --- 3. 비디오 옵션 (VBR 모드 분기) ---
        if self.v_mode == 'CRF':
            # [방식 1] CRF (1-Pass VBR)
            video_opts = ['-c:v', self.v_codec]
            if self.v_codec in ['libx264', 'libx265']:
                if self.v_crf: video_opts.extend(['-crf', self.v_crf])
                if self.v_preset: video_opts.extend(['-preset', self.v_preset])
            
            # 최종 1-Pass 명령어
            final_cmd = [*base_cmd, *video_opts, *audio_opts, *filter_opts, self.output_file]
            commands_to_run.append(final_cmd)
            
        elif self.v_mode == '2-Pass':
            # [방식 2] 2-Pass VBR (ABR)
            video_opts = ['-c:v', self.v_codec]
            if self.v_bitrate:
                video_opts.extend(['-b:v', self.v_bitrate]) # 평균 비트레이트 설정
            if self.v_preset:
                video_opts.extend(['-preset', self.v_preset])

            # Pass 1 명령어:
            # -pass 1 (1패스 모드)
            # -an (오디오 끔), -f null (출력 포맷 없음)
            # NUL (윈도우), /dev/null (리눅스/맥)
            null_device = 'NUL' if os.name == 'nt' else '/dev/null'
            pass1_cmd = [*base_cmd, *video_opts, *filter_opts, 
                         '-pass', '1', '-an', '-f', 'null', null_device]
            commands_to_run.append(pass1_cmd)
            
            # Pass 2 명령어:
            # -pass 2 (2패스 모드)
            # 여기서는 audio_opts를 포함하여 오디오도 함께 인코딩.
            pass2_cmd = [*base_cmd, *video_opts, *audio_opts, *filter_opts,
                         '-pass', '2', self.output_file]
            commands_to_run.append(pass2_cmd)
            
        else: # 'copy'
            video_opts = ['-c:v', 'copy']
            final_cmd = [*base_cmd, *video_opts, *audio_opts, *filter_opts, self.output_file]
            commands_to_run.append(final_cmd)

        return commands_to_run

# --- [Part 2] 범용 워커 (Context) (run 메서드 수정) ---

class GenericFFmpegWorker(QThread):
    """[v6.0] 여러 개의 명령어를 순차 실행할 수 있는 범용 워커"""
    progress_signal = Signal(str)
    finished_signal = Signal(str)
    error_signal = Signal(str)

    def __init__(self, job: BaseMediaJob):
        super().__init__()
        self.job = job
        self.process = None
        self._is_stopped = False # 사용자가 중지했는지 확인

    def run(self):
        """[v6.0] build_commands()가 반환한 명령어 리스트를 순차 실행"""
        try:
            # 1. 명세서(job)에 따라 '명령어들의 리스트'를 생성
            command_list_group = self.job.build_commands()
            total_passes = len(command_list_group)
            
            # [v6.0] 명령어 리스트를 순회
            for i, command in enumerate(command_list_group):
                if self._is_stopped: # stop()이 호출되었으면 중단
                    break
                    
                pass_num = i + 1
                self.progress_signal.emit(f"\n--- Pass {pass_num} / {total_passes} 시작 ---")
                self.progress_signal.emit(f"실행 명령어: {' '.join(command)}")
                
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    encoding='utf-8',
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )

                for line in self.process.stdout:
                    if self._is_stopped:
                        break
                    self.progress_signal.emit(line.strip())
                
                self.process.wait()
                
                # [v6.0] 현재 패스가 실패하면 즉시 전체 작업 중단
                if not self._is_stopped and self.process.returncode != 0:
                    self.error_signal.emit(f"FFmpeg 작업 실패 (Pass {pass_num}). (Return code: {self.process.returncode})")
                    return # run() 메서드 즉시 종료
                
                if self._is_stopped:
                    self.progress_signal.emit(f"Pass {pass_num} 중단됨.")
                    return # run() 메서드 즉시 종료

            # [v6.0] 모든 패스가 성공적으로 완료됨
            if not self._is_stopped:
                self.finished_signal.emit(f"작업 완료! 파일이 {self.job.output_file}에 저장되었습니다.")
        
        except Exception as e:
            if not self._is_stopped:
                self.error_signal.emit(f"작업 실행 중 예외 발생: {str(e)}")
        finally:
            # [v6.0] 2-pass 로그 파일 등을 정리
            cleanup_msg = self.job.cleanup()
            if cleanup_msg:
                self.progress_signal.emit(f"\n--- 정리 작업 ---\n{cleanup_msg}")
    
    def stop(self):
        self._is_stopped = True # [v6.0] 중지 플래그 설정
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.progress_signal.emit("작업 중단 신호 전송...")

# --- [Part 3] GUI 클라이언트 (Client) (Recode 탭 동적 UI 추가) ---

class VideoEditorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6 FFmpeg 편집기 (v6.0 - 2-Pass VBR 지원)")
        self.setGeometry(100, 100, 800, 700)
        
        self.worker = None

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.merge_tab = QWidget()
        self.tabs.addTab(self.merge_tab, "🎬 비디오 병합")
        self.create_merge_tab() 

        self.recode_tab = QWidget()
        self.tabs.addTab(self.recode_tab, "⚙️ 재인코딩")
        self.create_recode_tab() # [v6.0] 동적 UI로 변경

        main_layout.addWidget(QLabel("\n작업 로그:"))
        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        self.log_widget.setStyleSheet("background-color: #333; color: #eee; font-family: 'Consolas', 'Courier New';")
        main_layout.addWidget(self.log_widget)

    def create_merge_tab(self):
        # (v5.0과 동일 - 생략)
        layout = QVBoxLayout(self.merge_tab)
        layout.addWidget(QLabel("1. 병합할 비디오 파일 (순서 변경 가능):"))
        self.file_list_widget = QListWidget()
        self.file_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        layout.addWidget(self.file_list_widget)
        file_button_layout = QHBoxLayout()
        self.add_button = QPushButton("파일 추가")
        self.clear_button = QPushButton("목록 지우기")
        file_button_layout.addWidget(self.add_button)
        file_button_layout.addWidget(self.clear_button)
        layout.addLayout(file_button_layout)
        layout.addWidget(QLabel("\n2. 병합 방식 선택 및 실행:"))
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)
        merge_button_layout = QHBoxLayout()
        self.merge_demuxer_button = QPushButton("🚀 빠른 병합 (코덱/해상도 동일해야 함)")
        self.merge_filter_button = QPushButton("🐢 재인코딩 병합 (모든 파일 가능)")
        merge_button_layout.addWidget(self.merge_demuxer_button)
        merge_button_layout.addWidget(self.merge_filter_button)
        layout.addLayout(merge_button_layout)
        self.add_button.clicked.connect(self.add_files)
        self.clear_button.clicked.connect(self.clear_list)
        self.merge_demuxer_button.clicked.connect(lambda: self.start_merge(method='demuxer'))
        self.merge_filter_button.clicked.connect(lambda: self.start_merge(method='filter'))

    def create_recode_tab(self):
        """[v6.0] '재인코딩' 탭의 동적 UI 생성"""
        layout = QVBoxLayout(self.recode_tab)
        
        # 1. 입력 파일
        layout.addWidget(QLabel("1. 재인코딩할 입력 파일:"))
        input_layout = QHBoxLayout()
        self.recode_input_file = QLineEdit()
        self.recode_input_file.setReadOnly(True)
        self.recode_browse_button = QPushButton("파일 선택...")
        input_layout.addWidget(self.recode_input_file)
        input_layout.addWidget(self.recode_browse_button)
        layout.addLayout(input_layout)

        # 2. 인코딩 옵션 (그리드)
        layout.addWidget(QLabel("\n2. 인코딩 옵션 설정:"))
        options_layout = QGridLayout()
        
        # --- 비디오 옵션 ---
        options_layout.addWidget(QLabel("비디오 코덱:"), 0, 0)
        self.v_codec_combo = QComboBox()
        self.v_codec_combo.addItems(['libx264 (H.264)', 'libx265 (H.265)', 'copy (원본 복사)'])
        options_layout.addWidget(self.v_codec_combo, 0, 1)

        options_layout.addWidget(QLabel("비디오 모드:"), 1, 0)
        self.v_mode_combo = QComboBox()
        self.v_mode_combo.addItems(['CRF (품질 기준 VBR)', '2-Pass (비트레이트 기준 VBR)'])
        options_layout.addWidget(self.v_mode_combo, 1, 1)

        self.label_v_crf = QLabel("품질 (CRF, 0-51):")
        self.v_crf_input = QLineEdit("23")
        options_layout.addWidget(self.label_v_crf, 2, 0)
        options_layout.addWidget(self.v_crf_input, 2, 1)

        self.label_v_bitrate = QLabel("평균 비트레이트 (예: 5000k):")
        self.v_bitrate_input = QLineEdit("5000k")
        options_layout.addWidget(self.label_v_bitrate, 3, 0)
        options_layout.addWidget(self.v_bitrate_input, 3, 1)

        self.label_v_preset = QLabel("속도 (Preset):")
        self.v_preset_combo = QComboBox()
        self.v_preset_combo.addItems(['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'])
        self.v_preset_combo.setCurrentText('medium')
        options_layout.addWidget(self.label_v_preset, 4, 0)
        options_layout.addWidget(self.v_preset_combo, 4, 1)

        # --- [!!! 핵심 수정 1 !!!] ---
        # 비디오 옵션 초기 상태 설정 (기본값 'CRF' 기준)
        self.label_v_crf.setVisible(True)
        self.v_crf_input.setVisible(True)
        self.label_v_bitrate.setVisible(False) # 비디오 Bitrate 숨김
        self.v_bitrate_input.setVisible(False)
        
        # --- 오디오 옵션 (오른쪽 열) ---
        options_layout.addWidget(QLabel("오디오 코덱:"), 0, 2)
        self.a_codec_combo = QComboBox()
        self.a_codec_combo.addItems(['aac (권장)', 'libmp3lame (MP3)', 'copy (원본 복사)'])
        options_layout.addWidget(self.a_codec_combo, 0, 3)
        
        self.label_a_mode = QLabel("오디오 모드:")
        self.a_mode_combo = QComboBox()
        self.a_mode_combo.addItems(['ABR (평균 비트레이트)', 'VBR (품질 기준)'])
        options_layout.addWidget(self.label_a_mode, 1, 2)
        options_layout.addWidget(self.a_mode_combo, 1, 3)

        # [라벨 수정] "오디오" 명시
        self.label_a_bitrate = QLabel("오디오 비트레이트 (예: 128k):") 
        self.a_bitrate_input = QLineEdit("128k")
        options_layout.addWidget(self.label_a_bitrate, 2, 2)
        options_layout.addWidget(self.a_bitrate_input, 2, 3)

        # [라벨 수정] "오디오" 명시
        self.label_a_qscale = QLabel("오디오 품질 (VBR -q:a, 0-9):") 
        self.a_qscale_input = QLineEdit("4")
        options_layout.addWidget(self.label_a_qscale, 3, 2)
        options_layout.addWidget(self.a_qscale_input, 3, 3)
        
        # --- [!!! 핵심 수정 2 !!!] ---
        # 오디오 옵션 초기 상태 설정 (기본값 'aac' 기준)
        self.label_a_mode.setVisible(False) # 'aac'는 모드 변경 없음
        self.a_mode_combo.setVisible(False)
        self.label_a_bitrate.setVisible(True) # 'aac'는 ABR(Bitrate) 사용
        self.a_bitrate_input.setVisible(True)
        self.label_a_qscale.setVisible(False) # 'aac'는 qscale 사용 안 함
        self.a_qscale_input.setVisible(False)
                
        # --- 해상도 옵션 (아래쪽) ---
        options_layout.addWidget(QLabel("해상도 (예: 1280:-2):"), 5, 0)
        self.scale_input = QLineEdit()
        self.scale_input.setPlaceholderText("비워두면 원본 해상도 유지")
        options_layout.addWidget(self.scale_input, 5, 1, 1, 3) # 1행 3열 병합

        layout.addLayout(options_layout)

        # 3. 실행 버튼
        layout.addWidget(QLabel("\n3. 실행:"))
        self.start_recode_button = QPushButton("🚀 재인코딩 실행")
        self.start_recode_button.setStyleSheet("background-color: #e0eeff;")
        layout.addWidget(self.start_recode_button)

        layout.addStretch() # 위젯들을 위로 밀어붙임

        # --- [재인코딩 탭] 시그널 연결 ---
        self.recode_browse_button.clicked.connect(self.browse_recode_input)
        self.start_recode_button.clicked.connect(self.start_recode)
        
        # [v6.0] 동적 UI를 위한 시그널 연결
        self.v_codec_combo.currentTextChanged.connect(self._on_v_codec_changed)
        self.v_mode_combo.currentTextChanged.connect(self._on_v_mode_changed)
        self.a_codec_combo.currentTextChanged.connect(self._on_a_codec_changed)
        self.a_mode_combo.currentTextChanged.connect(self._on_a_mode_changed)

    # --- [v6.0] 동적 GUI 제어 슬롯 ---
    def _on_v_codec_changed(self, text):
        """비디오 코덱 콤보박스 변경 시 호출되는 슬롯"""
        is_copy = 'copy' in text
        # 'copy'가 선택되면 모든 비디오 옵션 숨김
        self.v_mode_combo.setVisible(not is_copy)
        self.label_v_preset.setVisible(not is_copy)
        self.v_preset_combo.setVisible(not is_copy)
        if is_copy:
            self.label_v_crf.setVisible(False)
            self.v_crf_input.setVisible(False)
            self.label_v_bitrate.setVisible(False)
            self.v_bitrate_input.setVisible(False)
        else:
            # 'copy'가 아니면, v_mode에 따라 다시 결정
            self._on_v_mode_changed(self.v_mode_combo.currentText())

    def _on_v_mode_changed(self, text):
        """비디오 모드 (CRF/2-Pass) 콤보박스 변경 시 호출되는 슬롯"""
        if not self.v_mode_combo.isVisible():
             return # 코덱이 'copy'라서 숨겨진 상태면 무시
             
        is_crf = 'CRF' in text
        # CRF 모드일 때: CRF 입력창 보이기, Bitrate 입력창 숨기기
        self.label_v_crf.setVisible(is_crf)
        self.v_crf_input.setVisible(is_crf)
        # 2-Pass 모드일 때: Bitrate 입력창 보이기, CRF 입력창 숨기기
        self.label_v_bitrate.setVisible(not is_crf)
        self.v_bitrate_input.setVisible(not is_crf)

    def _on_a_codec_changed(self, text):
        """오디오 코덱 콤보박스 변경 시 호출되는 슬롯"""
        is_copy = 'copy' in text
        is_mp3 = 'libmp3lame' in text
        
        # 'copy' 모드면 모든 오디오 옵션 숨김
        self.label_a_mode.setVisible(not is_copy)
        self.a_mode_combo.setVisible(not is_copy)
        if is_copy:
            self.label_a_bitrate.setVisible(False)
            self.a_bitrate_input.setVisible(False)
            self.label_a_qscale.setVisible(False)
            self.a_qscale_input.setVisible(False)
        else:
            # 'aac'는 VBR 옵션이 없으므로 ABR(Bitrate)만
            if not is_mp3: 
                self.a_mode_combo.setVisible(False) # MP3가 아니면 모드 선택 숨김
                self.label_a_mode.setVisible(False)
                self.label_a_bitrate.setVisible(True)
                self.a_bitrate_input.setVisible(True)
                self.label_a_qscale.setVisible(False)
                self.a_qscale_input.setVisible(False)
            else: # MP3는 모드 선택 가능
                self.a_mode_combo.setVisible(True)
                self.label_a_mode.setVisible(True)
                self._on_a_mode_changed(self.a_mode_combo.currentText())

    def _on_a_mode_changed(self, text):
        """오디오 모드 (ABR/VBR) 콤보박스 변경 시 호출되는 슬롯"""
        if not self.a_mode_combo.isVisible():
            return # 코덱이 'copy'거나 'aac'라서 숨겨진 상태면 무시
            
        is_vbr = 'VBR' in text
        # VBR(품질) 모드: qscale 입력창 보이기, bitrate 입력창 숨기기
        self.label_a_qscale.setVisible(is_vbr)
        self.a_qscale_input.setVisible(is_vbr)
        # ABR(비트레이트) 모드: bitrate 입력창 보이기, qscale 입력창 숨기기
        self.label_a_bitrate.setVisible(not is_vbr)
        self.a_bitrate_input.setVisible(not is_vbr)

    # --- [병합 탭] 슬롯 (v5.0과 동일) ---
    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "병합할 비디오 파일 선택", "", "비디오 파일 (*.mp4 *.mkv *.avi *.mov);;모든 파일 (*.*)")
        if files: self.file_list_widget.addItems(files)

    def clear_list(self):
        self.file_list_widget.clear()

    def start_merge(self, method):
        file_count = self.file_list_widget.count()
        if file_count < 2:
            QMessageBox.warning(self, "오류", "병합할 파일이 2개 이상 필요합니다.")
            return
        file_list = [self.file_list_widget.item(i).text() for i in range(file_count)]
        output_file, _ = QFileDialog.getSaveFileName(self, "저장할 파일 이름 선택", "", "MP4 비디오 (*.mp4);;모든 파일 (*.*)")
        if not output_file: return
        job = MergeDemuxerJob(file_list, output_file) if method == 'demuxer' else MergeFilterJob(file_list, output_file)
        self.start_worker(job)

    # --- [재인코딩 탭] 슬롯 (v6.0 - 옵션 수집 변경) ---
    def browse_recode_input(self):
        file, _ = QFileDialog.getOpenFileName(self, "재인코딩할 파일 선택", "", "비디오 파일 (*.mp4 *.mkv *.avi *.mov);;모든 파일 (*.*)")
        if file: self.recode_input_file.setText(file)

    def start_recode(self):
        # 1. GUI에서 모든 옵션 값 수집
        input_file = self.recode_input_file.text()
        if not input_file:
            QMessageBox.warning(self, "오류", "입력 파일을 선택해주세요.")
            return
        output_file, _ = QFileDialog.getSaveFileName(self, "저장할 파일 이름 선택", "", "MP4 비디오 (*.mp4);;MKV 비디오 (*.mkv);;모든 파일 (*.*)")
        if not output_file: return
            
        # 2. [v6.0] 모든 옵션 수집
        job = RecodeJob(
            input_file=input_file, 
            output_file=output_file,
            v_codec=self.v_codec_combo.currentText().split(' ')[0],
            v_mode=self.v_mode_combo.currentText().split(' ')[0],
            v_crf=self.v_crf_input.text(),
            v_preset=self.v_preset_combo.currentText(),
            v_bitrate=self.v_bitrate_input.text(),
            a_codec=self.a_codec_combo.currentText().split(' ')[0],
            a_mode=self.a_mode_combo.currentText().split(' ')[0],
            a_bitrate=self.a_bitrate_input.text(),
            a_qscale=self.a_qscale_input.text(),
            scale=self.scale_input.text()
        )
        self.start_worker(job) # 3. 범용 워커에게 'Job' 전달

    # --- [공통] 워커 실행 및 슬롯 (v5.0과 동일) ---
    def start_worker(self, job_to_run: BaseMediaJob):
        self.set_ui_enabled(False)
        self.log_widget.clear()
        self.worker = GenericFFmpegWorker(job_to_run)
        self.worker.progress_signal.connect(self.update_log)
        self.worker.finished_signal.connect(self.job_finished)
        self.worker.error_signal.connect(self.job_error)
        self.worker.start()

    def update_log(self, message):
        self.log_widget.append(message)
        self.log_widget.ensureCursorVisible()

    def job_finished(self, message):
        self.log_widget.append(f"\n--- 작업 완료 ---")
        self.log_widget.append(message)
        QMessageBox.information(self, "성공", message)
        self.set_ui_enabled(True)
        self.worker = None

    def job_error(self, message):
        self.log_widget.append(f"\n--- 작업 실패 ---")
        self.log_widget.append(message)
        QMessageBox.critical(self, "오류", message)
        self.set_ui_enabled(True)
        self.worker = None

    def set_ui_enabled(self, enabled):
        self.tabs.setEnabled(enabled)
        
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        event.accept()

# --- [Part 4] 프로그램 실행 ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoEditorApp()
    window.show()
    sys.exit(app.exec())