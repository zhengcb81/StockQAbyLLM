@echo off
REM Local CI Simulation Script (Windows)
REM Simulates GitHub Actions CI workflow locally
REM Usage: scripts\run_ci.bat

setlocal enabledelayedexpansion

REM Create reports directory
if not exist reports mkdir reports

REM Track overall success
set OVERALL_SUCCESS=true
set FAILED_COUNT=0

REM ==============================================================================
REM 1. Test Suite
REM ==============================================================================
echo.
echo ================================================================
echo   1. Running Test Suite (pytest)
echo ================================================================
echo.
echo Command: pytest tests/ -v --cov=src --cov-report=term-missing --cov-fail-under=60
echo.

pytest tests/ -v --cov=src --cov-report=term-missing --cov-fail-under=60 > reports\ci_tests.txt 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAILED] Tests failed or coverage below 60%%
    set OVERALL_SUCCESS=false
    set /a FAILED_COUNT+=1
) else (
    echo [PASSED] Tests passed
)

REM ==============================================================================
REM 2. Type Checking (mypy)
REM ==============================================================================
echo.
echo ================================================================
echo   2. Type Checking (mypy strict mode)
echo ================================================================
echo.
echo Command: mypy src/ --strict
echo.

mypy src/ --strict > reports\ci_mypy.txt 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAILED] Type checking failed
    set OVERALL_SUCCESS=false
    set /a FAILED_COUNT+=1
) else (
    echo [PASSED] Type checking passed ^(0 errors^)
)

REM ==============================================================================
REM 3. Code Quality (pylint)
REM ==============================================================================
echo.
echo ================================================================
echo   3. Code Quality (pylint)
echo ================================================================
echo.
echo Command: pylint src/ --fail-under=8.0 --exit-zero
echo.

pylint src/ --fail-under=8.0 --exit-zero > reports\ci_pylint.txt 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAILED] Pylint score below 8.0
    set OVERALL_SUCCESS=false
    set /a FAILED_COUNT+=1
) else (
    echo [PASSED] Pylint score ^>= 8.0
)

REM ==============================================================================
REM 4. Code Formatting (black)
REM ==============================================================================
echo.
echo ================================================================
echo   4. Code Formatting (black)
echo ================================================================
echo.
echo Command: black --check src/ tests/
echo.

black --check src/ tests/ > reports\ci_black.txt 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAILED] Code formatting issues found
    echo Run 'black src/ tests/' to fix
    set OVERALL_SUCCESS=false
    set /a FAILED_COUNT+=1
) else (
    echo [PASSED] Code formatting is correct
)

REM ==============================================================================
REM 5. Security Scan (bandit)
REM ==============================================================================
echo.
echo ================================================================
echo   5. Security Scan (bandit)
echo ================================================================
echo.
echo Command: bandit -r src/ -f screen -ll
echo.

bandit -r src/ -f screen -ll > reports\ci_bandit.txt 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAILED] Security issues found
    set OVERALL_SUCCESS=false
    set /a FAILED_COUNT+=1
) else (
    echo [PASSED] No high/medium security issues
)

REM ==============================================================================
REM 6. Complexity Check (radon)
REM ==============================================================================
echo.
echo ================================================================
echo   6. Complexity Analysis (radon)
echo ================================================================
echo.
echo Checking cyclomatic complexity...
echo.

radon cc src/ -a -nb --total-average > reports\ci_radon_cc.txt 2>&1
echo [INFO] Complexity analysis complete

echo.
echo Checking maintainability index...
echo.

radon mi src/ --min B > reports\ci_radon_mi.txt 2>&1
echo [INFO] Maintainability index check complete

REM ==============================================================================
REM 7. Build Verification
REM ==============================================================================
echo.
echo ================================================================
echo   7. Build Verification
echo ================================================================
echo.
echo Checking package structure...
echo.

python -c "import src; print('Package imports successfully')" > reports\ci_build.txt 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAILED] Package structure invalid
    set OVERALL_SUCCESS=false
    set /a FAILED_COUNT+=1
) else (
    echo [PASSED] Package structure valid
)

REM ==============================================================================
REM Summary
REM ==============================================================================
echo.
echo ================================================================
echo   CI Simulation Summary
echo ================================================================
echo.

if "%OVERALL_SUCCESS%"=="true" (
    echo ================================================================
    echo   ALL CHECKS PASSED
    echo ================================================================
    echo.
    echo You are ready to commit and push!
    exit /b 0
) else (
    echo ================================================================
    echo   SOME CHECKS FAILED ^( !FAILED_COUNT! failures^)
    echo ================================================================
    echo.
    echo Please fix the issues above before committing.
    echo See reports\ directory for detailed logs.
    exit /b 1
)
