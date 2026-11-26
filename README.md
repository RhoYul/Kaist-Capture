# PhysicalAI KAIST Platform — 촬영 PC · NAS 저장 · 관제

RealSense 4대의 캘리브레이션 → 5초 RGB-D 촬영 → 기존 Capture QC → 검수 승인 → 별도 서버 전달 → NAS 저장(PhysicalAI rgbd_raw 와 같은 원본 형태)을 위한 첫 구현입니다.

## 구성

```
[촬영 PC]  카메라 4대
   ├ 촬영 서버  127.0.0.1:8765   작업자 화면 (캘리브레이션 · 촬영 · QC · 검수 · 촬영 로그)
   └ export 서버 0.0.0.0:8766    NAS 저장 (같은 PC 지름길) · 관제 PC 가 붙는 곳
                     ▲ 서버 토큰
[관제 PC]  관제 서버 127.0.0.1:8767  관리자 노트북 · 이 PC 에서만 (촬영 PC 들 상태 · 작업 생성 · NAS 저장 · 로그)
```

| 화면 / 프로세스 | 역할 |
|---|---|
| 촬영 서버 :8765 | ChArUco 보정, 4분할 미리보기(상시 연결), 즉시 5초 촬영, 로컬 QC, 프레임 재생 검수, NAS 로 넘기기, 저장 공간 표시, 촬영 로그 |
| export 서버 :8766 | 작업 관리, 촬영 서버 상태 · 로그 수집, 원본 검증 · NAS 복사 · 체크섬 확인, 실패 재시도, 관제 PC 용 API |
| 관제 서버 :8767 | 여러 촬영 PC 의 export 서버를 한 화면에서: 연결 · 카메라 · 저장 공간 · 작업 생성/종료 · NAS 저장 현황 · 재시도 · 로그 검색 |

- **같은 PC 지름길**: export 서버가 같은 PC 면 원본 폴더를 바로 검증해 NAS 로 복사합니다 (ZIP 묶기 · HTTP 전송 · 압축 풀기 없음). 다른 PC 의 export 서버로 보낼 때만 ZIP 전송을 씁니다.
- **녹화 우선**: 녹화하는 동안(저장 끝날 때까지) NAS 복사 · 체크섬 읽기 · ZIP 묶기를 멈췄다가 이어 갑니다.
- **원본 자동 삭제**: NAS 복사 후 파일별 체크섬이 맞는 것이 확인되면 촬영 PC 원본을 지웁니다 (`delete_after_nas`). 촬영 화면에 남은 공간 · 촬영 가능 개수를 보여 주고, `min_free_gb`(기본 10GB) 보다 적으면 촬영을 막습니다. 보내지 않을 에피소드는 검수 창에서 삭제합니다.

## 새 PC 에 설치

받는 PC 에는 **Python 설치도, 인터넷도, PhysicalAI 도 필요 없습니다.** zip 안에 전용 Python(`python\`, 필요한 패키지만 · 약 400 MB)이 들어 있습니다.

1. 패키지 만들기 (이 노트북):
   - `<패키지가 깔린 python.exe> tools/build_runtime.py` → `dist\runtime\python` (전용 Python. 패키지 버전을 바꿀 때만 다시)
   - `python tools/build_release.py` → `dist\PhysicalAI_KAIST-날짜.zip` (약 145 MB · 설정 · 원본 · 로그는 빠짐)
2. **촬영 PC**: zip 을 `C:\` 에 풀면 `C:\PhysicalAI_KAIST` 가 생깁니다. 그 안의 `Setup-CapturePC.cmd` 실행.
   전용 Python 확인 → 촬영 PC 이름 · NAS 폴더 · 원본 임시 폴더 입력 → 방화벽 8766 열기(관리자 승인) → 로그온 자동 실행 → 서버 켜기.
   마지막에 **관제 PC 에 등록할 주소(컴퓨터 이름)와 토큰**이 나옵니다. 처음 쓰는 PC 는 캘리브레이션을 새로 하세요.
   PhysicalAI 가 설치된 PC 면 PhysicalAI 서버를 끄고 자동 실행을 해제해야 카메라를 쓸 수 있습니다.
3. **관제 PC (노트북)**: 같은 zip 을 풀고 `Setup-ControlPC.cmd` 실행 → http://localhost:8767 → 「＋ 촬영 PC 등록」에 주소 · 토큰 입력.
   관제 PC 는 고정 IP 가 필요 없습니다 (관제 PC 가 촬영 PC 에 붙음). 촬영 PC 는 컴퓨터 이름으로 찾습니다.

## 연속 촬영 · QC 일괄 실행 · 자동 NAS 전송

1. 촬영을 쉬지 않고 이어서 한다. **보내지 않은 촬영(QC 전 · QC 중 · 검수 필요)이 10개 쌓이면 촬영 버튼이 막힌다** (`capture.json` 의 `max_pending`, 0 이면 제한 없음).
2. 촬영 기록 위 「**QC 일괄 실행**」 → QC 전 촬영을 모두 대기열에 넣는다.
3. QC 는 **export 서버(같은 PC 8766)** 가 PhysicalAI 원본 QC 를 별도 프로세스로 낮은 우선순위에서 돌린다 (`qc_workers`, 기본 2개 동시).
   촬영 서버는 촬영만 하므로 QC 중에도 계속 촬영할 수 있고, **녹화가 시작되면 돌던 QC 를 바로 멈췄다가 녹화가 끝나면 처음부터 다시 돌린다** (녹화 프레임 보호).
4. 결과:
   - **QC 항목이 전부 PASS** → 자동 승인(「자동 승인」 표시) → NAS 로 자동 전송 → NAS 확인 뒤 이 PC 원본 삭제.
   - **WARN · FAIL 이 하나라도 있음** → 전송하지 않고 「**검수 필요**」. 작업자가 검수 화면에서 사유를 적어 승인 · 전송하거나, 삭제 후 재촬영.
   - 자동 전송을 끄려면 `capture.json` 에 `"auto_export": false` (그러면 PASS 도 작업자가 승인).
5. export 서버가 다른 PC 인 구성이면 QC 는 예전처럼 촬영 서버에서 하나씩 돈다 (그동안 촬영은 막힘).

## 촬영 해상도

기본 **848×480@30** (컬러 · 깊이 같음, 4대 공통). PhysicalAI MANO 손 피팅(`phystwin/Mono/data_process/data_process_hands.py` 의 `RES = [848., 480.]`)이
848×480 기준이라 맞춘 값입니다. (PhysicalAI 촬영 코드는 2026-11-10 에 640×480 으로 바꿨지만 MANO 쪽은 848 그대로라, 640 영상은 손 좌표가 가로로 1.33배 어긋납니다.)

- **카메라 4대 모두 USB 3 연결이어야 합니다.** USB 2.x 로 붙은 D455 는 848×480 을 낼 수 없습니다. 촬영 화면 카메라마다 `USB 3.2` 처럼 연결 종류가 보이고, USB 2 면 ⚠ 경고가 뜹니다.
- 바꾸려면 촬영 PC `production-config\capture.json` 에 `"capture_width": 640, "capture_height": 480` (가능: 640×480 · 848×480 · 1280×720, `capture_fps` 15/30) 을 넣고 촬영 서버를 다시 켭니다.
- **해상도를 바꾸면 캘리브레이션을 다시 해야 촬영할 수 있습니다** (extrinsics.json 의 intrinsics 가 영상 해상도와 같아야 하므로, 다른 해상도의 결과로는 촬영을 막습니다).
- 용량: 에피소드(5초)당 약 30% 증가 (깊이 약 370 MB → 490 MB).

## 코드 업데이트 (관제 PC 에서 원격으로)

코드는 이 노트북(관제 PC)에서만 고칩니다. zip 을 다시 만들 필요 없이 관제 화면에서 촬영 PC 로 보냅니다.

1. 이 노트북의 `C:\capture-studio` 에서 코드를 고친다 (관제 화면 자체를 고쳤으면 `tools\Server.ps1 restart -Role control`).
2. 관제 화면(http://localhost:8767) 상단에 **이 PC 코드 버전**이 보이고, 촬영 PC 카드마다 **버전 · 최신/구버전**이 보인다.
3. 카드의 「업데이트」 또는 「⟳ 모두 업데이트」 → 촬영 PC 가 받아서
   서버 정상 종료 → 지금 코드를 `updates\backup` 에 보관 → 새 코드로 교체 → 서버 켜기 → 새 버전 확인 (보통 20~30초).
   **새 코드로 서버가 안 켜지면 스스로 이전 코드로 되돌리고** 카드에 「되돌림」과 이유가 뜬다 (`logs\update.log`, `logs\*.err.log`).
4. 녹화 · QC · 캘리브레이션 · NAS 저장 중인 PC 는 업데이트를 받지 않는다 (끝난 뒤 다시 누름).

보내는 것: `studio` · `tools` · `tests` 폴더와 최상위 `*.cmd` · README 등 소스만 (파일마다 SHA256 확인).
보내지 않는 것: 전용 Python(`python\`), 설정(비밀번호 · 토큰), 원본 · DB · 로그.
**Python 패키지를 바꾸거나 추가했을 때만** `build_runtime.py` → `build_release.py` 로 zip 을 다시 만들어 촬영 PC 에 새로 풀어야 합니다.
업데이트 API 는 서버 토큰으로만 열리며(관제 PC 등록 때 쓰는 그 토큰), 촬영 PC 의 소스 폴더 밖 경로는 받지 않습니다.

별도 프론트엔드 빌드가 필요 없는 FastAPI + HTML/CSS/JS 구성입니다. 외부 폰트/CDN 없이 실행합니다. GPU는 사용하지 않습니다.

## 원본 코드에서 가져온 부분

- studio/vendor/calibration.py: 기존 ChArUco/체커보드 계산. **수식, 코너 조건, 계산 방식, 반환 오차를 변경하지 않았습니다.** Windows에서도 실행되도록 진단 이미지 경로 /tmp/calib_debug만 OS 임시 폴더로 변경했습니다.
- studio/vendor/capture_qc.py: **원본 파일 그대로**. 프레임 연속성, 색상 품질, 깊이 완전성, 카메라 간 잔차, 월드 평면, 시간차 검사와 판정을 유지했습니다.
- studio/presets.py: 기존 1D/2D/3D QC 설정 그대로.
- studio/vendor/realsense.py, camera.py: 기존 촬영·미리보기 코드 (파일은 원본 그대로).
- studio/warm.py: 즉시 녹화. 카메라 4대를 녹화와 같은 설정(컬러+depth 848×480@30, align)으로 상시 열어 두고 미리보기도 같은 스트림을 쓴다. 버튼을 누르면 같은 호스트 시각부터 4대가 기록한다 (실측: 시작 0.02초, 카메라 간 시작 차이 10~46ms; 예전 방식은 약 9초 · 최대 1.4초). 파일 쓰기는 vendor/realsense.py 의 RealSenseRecorder 쓰기 스레드를 그대로 쓰고, intrinsics.json 은 녹화 첫 프레임의 스트림 값으로 원본과 같은 형식으로 쓴다. vendor/camera.py 미리보기는 더 이상 쓰지 않는다.
- SOURCE_PROVENANCE.json: 원본 위치, 원본 SHA256, 포함된 파일 SHA256 및 유일한 보정 경로 변경 기록.

기존 화면과 같이 재투영 오차는 작업자가 확인합니다. 오차 임계값을 새로 강제하지 않았습니다. 확인 버튼으로 승인한 보정만 촬영에 사용하며, 카메라 시리얼 구성이 바뀌면 다시 보정해야 합니다. 카메라 위치 변경은 자동 검출하지 못합니다.

원본 폴더의 화면에는 Depth ICP 버튼이 있지만 연결된 Python API 구현을 찾지 못했습니다. 없는 구현을 새 알고리즘으로 대체하지 않았으며 **이 버전은 검증 가능한 ChArUco 경로만 연결**했습니다. 기존 실제 실행 환경에서 Depth ICP를 사용했다면 해당 구현을 추가로 확보해야 합니다.

## 설치

새 PC 는 위 「새 PC 에 설치」대로 zip 의 전용 Python 을 씁니다 (Server.ps1 이 `python\python.exe` 를 가장 먼저 찾음). 아래는 직접 Python 환경을 꾸밀 때입니다.
Python 3.10 이상 권장. 촬영 PC에는 RealSense SDK Python 모듈이 필요합니다. OpenCV는 aruco.CharucoDetector가 있는 contrib 버전을 사용하세요. 기존 설치본의 Python 환경으로 실행 검증했습니다.

일반적인 새 환경:

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:STUDIO_PYTHON = "$PWD\.venv\Scripts\python.exe"
~~~

이 PC에 이미 있는 Python을 사용하려면 (기존 환경의 패키지를 수정하지 않습니다):

~~~powershell
$env:STUDIO_PYTHON = "$env:USERPROFILE\Desktop\ui_install\python\python.exe"
& $env:STUDIO_PYTHON -m studio init --demo
~~~

설정 생성 시 관리자·작업자 비밀번호가 출력됩니다. 같은 설정 폴더를 덮어쓰지 않습니다. config/*.json은 비밀번호와 서버 토큰을 포함하므로 배포·공유하지 마세요.

## 먼저 데모 확인

프로젝트 폴더에서:

~~~powershell
python -m studio init --demo
python -m studio serve --config config/export.json
~~~

다른 터미널에서:

~~~powershell
python -m studio serve --config config/capture.json
~~~

아래 실행 파일로 데모를 켜려면 먼저 `set STUDIO_CONFIG=config` 를 하세요 (기본은 실장비 설정 production-config). 새 ZIP을 푼 경우 init을 먼저 실행합니다.

## 실장비 실행 · 재시작 (이 PC)

더블클릭하면 서버가 **창 없이 백그라운드로** 켜지고, 실행 창은 3초 뒤 저절로 닫힌다.

| 파일 | 하는 일 |
|---|---|
| Start-PhysicalAI_KAIST.cmd | 이 PC 에 설정된 서버를 켠다 (촬영 PC: export 8766 → 촬영 8765 / 관제 PC: 관제 8767). 이미 켜져 있으면 그대로 둔다 |
| Stop-PhysicalAI_KAIST.cmd | 서버를 끈다. 촬영 서버는 카메라를 정상적으로 닫고 끝난다 |
| Restart-Capture.cmd | **카메라가 잘 안 나올 때.** 촬영 PC 서버만 정상 종료(카메라 닫기) 후 다시 켠다 |
| Status-PhysicalAI_KAIST.cmd | 켜져 있는지 · 카메라 연결 수를 보여 준다 |

- 모두 `tools\Server.ps1 start|stop|restart|status [-Role all|capture|export]` 를 부른다.
- 끄기는 서버에 정상 종료를 요청하는 방식이다 (이 PC 에서 서버 토큰으로만 가능). 15초 안에 안 꺼질 때만 강제 종료하며, 촬영 · QC · 캘리브레이션 · 전송 · NAS 저장 중이면 끄지 않고 멈춘다.
- 로그: `logs\capture.err.log`, `logs\export.err.log` (켤 때마다 직전 로그는 `.prev` 로 남김).
- Python 은 `STUDIO_PYTHON` → `Desktop\ui_install\python` → `C:\PhysicalAI\python` → `python` 순서로 찾는다. 데모 설정으로 켜려면 `set STUDIO_CONFIG=config`.
- 작업 관리자로 python 을 강제 종료하면 카메라가 멈춰 USB 를 다시 꽂아야 할 수 있다. 끌 때는 Stop / Restart 파일을 쓴다.
- 서버를 켠 뒤 카메라를 꽂거나 다시 꽂아도 5초 안에 자동으로 다시 연결된다.
- PC 재부팅 뒤에는 Start-PhysicalAI_KAIST.cmd 를 다시 실행한다 (Setup 에서 로그온 자동 실행을 켰으면 저절로 켜진다).

- 작업자: http://127.0.0.1:8765
- 관리자: http://127.0.0.1:8766/admin
- 데모: 합성 RGB·Depth, 모의 캘리브레이션, 모의 QC. 테스트용 저장소는 runtime/demo-nas. 실장비 정확도 검증이 아닙니다.
- 관리자로 작업 만들기 → 작업자로 로그인 → 보드 캡처 5번 → 보정 계산·확인 → 작업 선택 → 촬영 → 상세에서 QC 실행·확인 메모·승인 → 서버 전달.
- 데모/실장비 데이터가 다른 모드의 export 서버로 들어오면 실패 처리합니다.

## 설정 파일 (수동으로 바꿀 때)

Setup-CapturePC 는 `python -m studio init --directory production-config --station 이름 --nas-root NAS폴더 --data-dir 원본폴더 --remote-access` 로 설정을 만듭니다. 관제 PC 는 `python -m studio init-control` (control-config\control.json). 아래는 촬영 서버를 다른 PC 의 export 서버로 보내는 구성일 때의 값입니다 (기본 구성은 둘 다 촬영 PC 에서 돌며 export_url 은 http://127.0.0.1:8766).

### 촬영 PC: config/capture.json

- role: capture
- demo: false
- host: 127.0.0.1 (촬영 PC 자체 브라우저로 사용)
- port: 8765
- data_dir: 촬영 PC의 여유 공간이 충분한 로컬 SSD 절대 경로
- station: station-01 등 고유 이름
- export_url: http://EXPORT_PC_IP:8766
- transfer_token: export 서버와 같은 비밀 토큰
- worker_password / admin_password: 로그인 비밀번호

### Export PC: config/export.json

- role: export
- demo: false
- host: 0.0.0.0 (내부망에서 접근할 때)
- port: 8766
- data_dir: export PC의 로컬 SSD 절대 경로. NAS 경로를 넣지 마세요.
- nas_root: 이미 접근 가능한 NAS 공유 폴더의 절대 경로
- transfer_token: 촬영 PC와 같은 값

Windows JSON의 UNC 예시는 아래와 같습니다. 역슬래시는 JSON에서 두 번 표기해야 합니다.

~~~json
{"nas_root": "\\\\NAS-NAME\\share\\capture"}
~~~

### NAS 다이렉트 랜 연결 (촬영 PC ↔ NAS 케이블 직결)

공유기 · 스위치 없이 케이블로 바로 연결하면 DHCP 가 없어 양쪽이 169.254.x.x 자동 주소를 갖고, Windows 는 주소마다 NAS 로그인을 따로 저장하므로 예전 주소(`\\10.20.32.24`)의 로그인이 새 주소에는 쓰이지 않습니다.

- `Setup-DirectNAS.cmd` 실행: 케이블 꽂힌 어댑터 찾기(속도 경고) → NAS 주소(고정 IP 권장 / 자동 169.254) → 이 PC 고정 IP 설정(게이트웨이 없음 · Wi-Fi 인터넷 유지) → NAS 로그인 저장(cmdkey) → `export.json` 수정 → 쓰기 속도 확인 → export 서버 재시작.
- 고정 IP 권장: NAS DSM 「제어판 → 네트워크 → 네트워크 인터페이스 → 해당 LAN → 수동 구성」에서 예: `192.168.100.2 / 255.255.255.0`, 게이트웨이 비움. 촬영 PC 는 `192.168.100.1`. 사내망 대역(10.20.x.x)과 겹치지 않게.
- `export.json`: `nas_root` = 다이렉트 경로, `nas_root_fallback` = 예비 경로 목록 (예: `["\\\\10.20.32.24\\kaist-data\\rgbd_raw"]`). 저장할 때마다 `nas_root` 먼저(SMB 445 를 1초 확인), 안 되면 예비 경로로 저장하고 작업 로그에 경고를 남깁니다. **같은 NAS 의 같은 공유 폴더만** 넣으세요.
- 케이블은 1 GbE 이상 어댑터에 꽂으세요. 100 Mbps(USB FE 어댑터 등)는 초당 약 11 MB 로 Wi-Fi 보다 느릴 수 있습니다.

Python 실행 계정이 NAS 공유 폴더를 읽고 쓸 수 있어야 합니다. 서비스 계정에서는 탐색기의 매핑 드라이브가 보이지 않을 수 있으므로 UNC 경로를 사용하세요. NAS 계정 설정과 방화벽은 이 프로그램이 변경하지 않습니다.

이 버전은 사내망/VPN용입니다. 외부 인터넷 포트포워딩은 하지 마세요. 외부 접근이 필요하면 HTTPS 역방향 프록시 또는 VPN을 먼저 구성해야 합니다. 세션은 8시간이며 관리자와 작업자 권한은 API에서 구분됩니다. 관제 PC 와 export 서버 사이는 서버 토큰(HTTP)으로 통신하므로 사내망 안에서만 쓰세요.

## 처리 방식과 출력

1. 노트북: 기존 캡처 방식으로 RGB MP4 + 정렬된 uint16 Depth NPY + CSV + intrinsics 저장.
2. 촬영 종료 시 해당 보정 결과를 에피소드와 연결. 원본은 로컬에 계속 보관.
3. 노트북: 원본 QC 알고리즘 실행. PASS/WARN/FAIL 표시. WARN/FAIL을 승인할 때는 사유 필수. 검사 알고리즘 자체는 변경하지 않음.
4. 노트북: 원본을 무압축 ZIP 한 개로 묶고 SHA256 계산. 4MiB 청크로 서버 전송. 실패 시 버튼으로 재시도하면 이어받음.
5. Export PC: 묶음과 내부 파일 체크섬 검증. **변환하지 않습니다** — PhysicalAI 의 rgbd_raw 저장(copy_tree)과 같이 녹화 원본을 그대로 올립니다.
6. Export PC: NAS의 임시 .part 폴더로 복사. NAS 파일 체크섬 검증 후 최종 폴더명으로 변경. 이후에만 NAS 완료 표시.

NAS 출력은 PhysicalAI 가 `rgbd_raw/` 에 저장하던 녹화 원본 폴더와 같습니다 (PhysicalAI RGB-D 처리 서버가 같은 방식으로 인식).

| 경로 (nas_root / ep_YYYYMMDD_HHMMSS 아래, 녹화 시작 시각 UTC) | 내용 |
|---|---|
| cam_N.mp4 | RGB 영상 (848×480@30, mp4v · 해상도는 capture.json 설정) |
| cam_N_depth/frame_XXXXXX.npy | color 정렬 uint16 Depth, 원본 그대로 |
| cam_N_intrinsics.json | 해당 카메라 내부 파라미터 및 depth_scale |
| cam_N_timestamps.csv | frame_idx, hw_timestamp_ms, host_time_s |
| extrinsics.json | 캘리브레이션 결과 (PhysicalAI 와 같은 키) |
| rgbd_episode.json | 표식: Task · 물체 정보(object: prompt · label · scene · name · interaction) · 촬영 시작/종료 · 프레임 수 · 캘리브레이션 · 에피소드 UID (중계기 프로젝트/작업자 ID 는 비움) |

물체 정보(SAM 프롬프트 · 마스크 라벨 · 장면 · 납품 물체 이름 · 상호작용 설명)는 PhysicalAI 와 같이 **작업을 만들 때** 관리자 화면에서 입력합니다 (모두 필수).
촬영자 화면은 녹화할 때 고른 작업의 물체 정보를 rgbd_episode.json 의 object 에 그대로 기록합니다 (PhysicalAI worker-collection 과 같은 방식).
metadata.json 은 만들지 않습니다 — PhysicalAI 에서 metadata.json 은 Phase 1 전처리 결과이자 '전처리 완료' 표식이고, 납품 단계에서 object_name · interaction 이 합쳐집니다.

플랫폼 내부용 qc.json · episode.json 은 촬영 PC 에만 남고 NAS 에는 올리지 않습니다.

파일이 수신됐다는 사실과 NAS 저장 완료는 구분됩니다. 전송을 시작한 에피소드는 검수·QC 변경을 잠가 원본 묶음을 불변으로 유지합니다. 정정이 필요하면 새 에피소드로 촬영합니다. 같은 작업 ID의 다른 내용은 덮어쓰지 않습니다. NAS의 기존 결과도 무조건 덮어쓰지 않습니다.

## 관리자 기능

- 작업 생성, 장면 유형(1D/2D/3D), 목표 수량, 작업 종료·재개.
- 촬영 PC 5초 heartbeat, 카메라 연결 수, 최근 작업 상태.
- 촬영 PC 로그를 중앙으로 모아 표시(최근 100건씩 동기화; 장기 오프라인 동안의 모든 로그를 무제한 보장하지 않음).
- 수신 / 대기 / 검증 / NAS 저장 / NAS 검증 / 완료 / 실패 상태.
- 처리 단계별 시간 기록. 실패한 export 재시도.
- DB는 SQLite WAL로 영구 보관. 처리 중 서버가 재시작되면 실패 상태로 전환해 수동 재시도. 미완료 수신은 이어받기.

## 속도와 운영상 한계

- 촬영 PC는 RGB 인코딩, RGB-Depth 정렬, NPY 저장, 기존 QC를 계속 수행합니다. NAS 쓰기는 export 서버가 담당합니다.
- 원본 전송에는 노트북 디스크 읽기·묶기·네트워크가 필요합니다. 전송과 다음 촬영이 겹칠 때 USB/SSD 부하를 실제 장비에서 측정해야 합니다.
- 총 완료시간이 기존 1~3분보다 빨라졌다고 아직 주장하지 않습니다. 합성 데이터와 로컬 네트워크 테스트는 실제 NAS 성능을 대표하지 않습니다.
- NAS에는 무압축 depth .npy 를 프레임마다 저장합니다 (카메라당 프레임 약 600KB, 5초 에피소드 약 370MB). NAS 쓰기 · 검증 읽기 시간이 대부분입니다.
- 5초는 서버 측 촬영 구간 타이머입니다. 노출 안정화나 프레임 누락을 보정해 정확히 150개의 유효 프레임을 보장하지는 않습니다. 프레임 수는 기록하며 기존 QC로 확인합니다.
- 원본 캘리브레이션/QC의 알려진 특성도 유지됩니다: PC 도착 시각 기반 동기화 검사, 일부 검사 깊이 스케일 1/1000 가정, 일부 검사 SKIP 처리. 알고리즘 개선은 별도 변경으로 다뤄야 합니다.
- 로컬 원본, 전송 ZIP, 서버 수신본·작업본을 자동 삭제하지 않습니다. 여러 복사본을 위한 SSD 공간을 확보하고 NAS 완료를 확인한 뒤 운영자가 보관 정책에 따라 정리하세요.
- 이 버전은 목표 수량의 완료 건수 통계와 역할별 공유 비밀번호 기반입니다. 개인별 계정/SSO, 작업자 할당, 일정 계획, 데이터 삭제 UI는 포함하지 않습니다.

## 테스트

~~~powershell
python -m unittest discover -s tests -v
~~~

tests/smoke_demo.py: 데모 서버 2개를 실행한 상태에서 4대×150프레임 생성 → 모의 QC → 실제 묶음 전송 → 로컬 테스트 NAS 에 원본 형태로 저장·검증.

tests/browser_check.py: 선택적 Playwright 브라우저 검사. Python playwright와 Chrome 필요. 로그인, 상세 검수, 프레임 탐색, 관리자, 모바일 폭 검증.

TEST_REPORT.md에 이번 실행 결과와 미검증 항목을 기록했습니다. 실제 4대 연결, 보드 계산, 실제 장면 QC, 실제 NAS 성능·권한은 현장 검증이 남아 있습니다.
