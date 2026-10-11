# paho-mqtt 1.x 호환성 테스트 (라즈베리파이 Bookworm의 apt 패키지가 1.6.1)
#   .\scripts\test_paho1.ps1
# venv를 새로 만들지 않고 paho-mqtt 1.6.1만 .paho1 폴더에 설치한 뒤 PYTHONPATH로 우선 불러온다.
# MQTT 관련 테스트만 실행한다 (인식·카메라 테스트는 paho와 무관).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$target = Join-Path $root ".paho1"

if (-not (Test-Path (Join-Path $target "paho"))) {
    & $py -m pip install --quiet --target $target "paho-mqtt==1.6.1"
    if ($LASTEXITCODE -ne 0) { throw "paho-mqtt 1.6.1 설치 실패" }
}

$env:PYTHONPATH = $target
try {
    & $py -c "import paho.mqtt, sys; v = paho.mqtt.__version__; print('paho-mqtt', v); sys.exit(0 if v.startswith('1.') else 1)"
    if ($LASTEXITCODE -ne 0) { throw ".paho1의 paho-mqtt 1.x가 로드되지 않았습니다" }
    Push-Location $root
    & $py -m pytest -q tests/test_env.py tests/test_publisher.py tests/test_broker_security.py tests/test_scanner_app.py tests/test_subscriber.py tests/test_pipeline.py tests/test_db_tool.py tests/test_serial_bridge.py @args
    $code = $LASTEXITCODE
    Pop-Location
    exit $code
} finally {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
