import sys
import os
import subprocess
import tempfile
# import json # 아직 ffprobe는 사용하지 않습니다.
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QFileDialog, QTextEdit, QMessageBox,
    QLabel, QFrame
)
from PySide6.QtCore import QThread, Signal, Qt

class MergeWorker(QThread):
    # (시그널 정의는 v3.0과 동일)
    progress_signal = Signal(str)
    finished_signal = Signal(str)
    error_signal = Signal(str)

    def __init__(self, file_list, output_file, merge_method):
        super().__init__()
        self.file_list = file_list
        self.output_file = output_file
        self.merge_method = merge_method
        self.process = None
        self.temp_file_name = None

    def run(self):
        try:
            if self.merge_method == 'demuxer':
                self.run_demuxer_merge()
            elif self.merge_method == 'filter':
                self.run_filter_merge()
            else:
                self.error_signal.emit(f"알 수 없는 병합 방식: {self.merge_method}")

        except Exception as e:
            self.error_signal.emit(f"작업 실행 중 예외 발생: {str(e)}")
        finally:
            if self.temp_file_name and os.path.exists(self.temp_file_name):
                os.remove(self.temp_file_name)
                self.progress_signal.emit(f"임시 목록 파일 삭제: {self.temp_file_name}")

    def run_demuxer_merge(self):
        # (v3.0과 동일 - 변경 없음)
        self.progress_signal.emit("--- '빠른 병합 (Demuxer)' 작업을 시작합니다 ---")
        
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as temp_list_file:
            for file_path in self.file_list:
                safe_path = file_path.replace(os.sep, '/')
                temp_list_file.write(f"file '{safe_path}'\n")
            self.temp_file_name = temp_list_file.name
        
        self.progress_signal.emit(f"임시 목록 파일 생성: {self.temp_file_name}")

        command = [
            'ffmpeg', '-f', 'concat', '-safe', '0',
            '-i', self.temp_file_name,
            '-c', 'copy', '-y', self.output_file
        ]
        
        self.execute_ffmpeg_command(command)

    def run_filter_merge(self):
        """
        [방식 2] Concat Filter (v4.0 - 입력 순서 오류 수정)
        """
        self.progress_signal.emit("--- '재인코딩 병합 (Filter)' 작업을 시작합니다 ---")
        self.progress_signal.emit("스트림 정규화(해상도/오디오 포맷 통일)를 수행합니다.")

        # 1. 공통 '틀' (Target Format) 정의 (v3.0과 동일)
        TARGET_WIDTH = 1920
        TARGET_HEIGHT = 1080
        TARGET_SAR = "1" 
        TARGET_AUDIO_RATE = "44100"
        TARGET_AUDIO_LAYOUT = "stereo"

        # 2. 입력 파일 목록 (v3.0과 동일)
        input_args = []
        for file_path in self.file_list:
            input_args.extend(['-i', file_path])

        # --- [v3.0 -> v4.0 수정된 부분] ---
        
        # 3. -filter_complex 문자열 동적 구성 (정규화 + 올바른 순서)
        filter_parts = []
        concat_inputs = "" # [v0][a0][v1][a1]... (올바른 순서로 조합될 변수)
        file_count = len(self.file_list)

        for i in range(file_count):
            # [비디오 정규화] (v3.0과 동일)
            video_filter = (
                f"[{i}:v]"
                f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar={TARGET_SAR}"
                f"[v{i}]"
            )
            filter_parts.append(video_filter)

            # [오디오 정규화] (v3.0과 동일)
            audio_filter = (
                f"[{i}:a]"
                f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts={TARGET_AUDIO_LAYOUT}"
                f"[a{i}]"
            )
            filter_parts.append(audio_filter)

            # [핵심 수정]
            # v3.0에서는 비디오/오디오를 따로 모았으나,
            # v4.0에서는 [v0][a0], [v1][a1]... 순서로 바로 조합합니다.
            concat_inputs += f"[v{i}][a{i}]" 

        # 4. 정규화된 스트림들을 concat으로 병합 (올바른 순서로)
        concat_filter = (
            f"{concat_inputs}" # [v0][a0][v1][a1]... 순서
            f"concat=n={file_count}:v=1:a=1[v][a]"
        )
        filter_parts.append(concat_filter)

        # 5. 모든 필터 정의를 세미콜론(;)으로 연결 (v3.0과 동일)
        filter_string = ";".join(filter_parts)
        
        # --- [수정 완료] ---

        self.progress_signal.emit(f"사용될 정규화 필터: {filter_string}")

        # 3. FFmpeg 명령어 구성 (v3.0과 동일)
        command = ['ffmpeg']
        command.extend(input_args)
        command.extend([
            '-filter_complex', filter_string,
            '-map', '[v]', 
            '-map', '[a]', 
            '-y',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            self.output_file
        ])

        # 4. FFmpeg 프로세스 실행
        self.execute_ffmpeg_command(command)

    def execute_ffmpeg_command(self, command):
        # (v3.0과 동일 - 변경 없음)
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
            self.progress_signal.emit(line.strip())

        self.process.wait()

        if self.process.returncode == 0:
            self.finished_signal.emit(f"병합 완료! 파일이 {self.output_file}에 저장되었습니다.")
        else:
            self.error_signal.emit(f"FFmpeg 작업 실패. (Return code: {self.process.returncode})")

    def stop(self):
        # (v3.0과 동일 - 변경 없음)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.progress_signal.emit("작업이 중단되었습니다.")


class VideoMergerApp(QMainWindow):
    # (GUI 부분은 v3.0과 완벽히 동일 - 변경 없음)
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6 FFmpeg 비디오 병합기 (v4.0 - concat 순서 수정)")
        self.setGeometry(100, 100, 800, 700)
        
        self.worker = None

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        main_layout.addWidget(QLabel("1. 병합할 비디오 파일 (순서 변경 가능):"))
        self.file_list_widget = QListWidget()
        self.file_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        main_layout.addWidget(self.file_list_widget)

        file_button_layout = QHBoxLayout()
        self.add_button = QPushButton("파일 추가")
        self.clear_button = QPushButton("목록 지우기")
        file_button_layout.addWidget(self.add_button)
        file_button_layout.addWidget(self.clear_button)
        main_layout.addLayout(file_button_layout)

        main_layout.addWidget(QLabel("\n2. 병합 방식 선택 및 실행:"))
        
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        main_layout.addWidget(line)

        merge_button_layout = QHBoxLayout()
        self.merge_demuxer_button = QPushButton("🚀 빠른 병합 (코덱/해상도 동일해야 함)")
        self.merge_demuxer_button.setStyleSheet("background-color: #e0ffee;")
        
        self.merge_filter_button = QPushButton("🐢 재인코딩 병합 (모든 파일 가능)")
        self.merge_filter_button.setStyleSheet("background-color: #ffeee0;")

        merge_button_layout.addWidget(self.merge_demuxer_button)
        merge_button_layout.addWidget(self.merge_filter_button)
        main_layout.addLayout(merge_button_layout)

        main_layout.addWidget(QLabel("\n3. 작업 로그:"))
        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        self.log_widget.setStyleSheet("background-color: #333; color: #eee; font-family: 'Consolas', 'Courier New';")
        main_layout.addWidget(self.log_widget)

        self.add_button.clicked.connect(self.add_files)
        self.clear_button.clicked.connect(self.clear_list)
        
        self.merge_demuxer_button.clicked.connect(
            lambda: self.start_merge(method='demuxer')
        )
        self.merge_filter_button.clicked.connect(
            lambda: self.start_merge(method='filter')
        )

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "병합할 비디오 파일 선택", "",
            "비디오 파일 (*.mp4 *.mkv *.avi *.mov);;모든 파일 (*.*)"
        )
        if files:
            self.file_list_widget.addItems(files)
            self.log_widget.append(f"{len(files)}개 파일을 목록에 추가했습니다.")

    def clear_list(self):
        self.file_list_widget.clear()
        self.log_widget.append("파일 목록을 초기화했습니다.")

    def start_merge(self, method):
        file_count = self.file_list_widget.count()
        if file_count < 2:
            QMessageBox.warning(self, "오류", "병합할 파일이 2개 이상 필요합니다.")
            return

        file_list = [self.file_list_widget.item(i).text() for i in range(file_count)]

        output_file, _ = QFileDialog.getSaveFileName(
            self, "저장할 파일 이름 선택", "",
            "MP4 비디오 (*.mp4);;MKV 비디오 (*.mkv);;모든 파일 (*.*)"
        )

        if not output_file:
            self.log_widget.append("병합 작업을 취소했습니다.")
            return

        self.set_ui_enabled(False)
        self.log_widget.clear()

        self.worker = MergeWorker(file_list, output_file, method)
        
        self.worker.progress_signal.connect(self.update_log)
        self.worker.finished_signal.connect(self.merge_finished)
        self.worker.error_signal.connect(self.merge_error)
        
        self.worker.start()

    def update_log(self, message):
        self.log_widget.append(message)
        self.log_widget.ensureCursorVisible()

    def merge_finished(self, message):
        self.log_widget.append(f"\n--- 작업 완료 ---")
        self.log_widget.append(message)
        QMessageBox.information(self, "성공", message)
        self.set_ui_enabled(True)
        self.worker = None

    def merge_error(self, message):
        self.log_widget.append(f"\n--- 작업 실패 ---")
        self.log_widget.append(message)
        QMessageBox.critical(self, "오류", message)
        self.set_ui_enabled(True)
        self.worker = None

    def set_ui_enabled(self, enabled):
        self.file_list_widget.setEnabled(enabled)
        self.add_button.setEnabled(enabled)
        self.clear_button.setEnabled(enabled)
        self.merge_demuxer_button.setEnabled(enabled)
        self.merge_filter_button.setEnabled(enabled)
    
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoMergerApp()
    window.show()
    sys.exit(app.exec())