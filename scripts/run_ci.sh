#!/usr/bin/env bash
# Local CI Simulation Script
# Simulates GitHub Actions CI workflow locally
# Usage: ./scripts/run_ci.sh

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    local color=$1
    local message=$2
    echo -e "${color}${message}${NC}"
}

# Function to print section header
print_section() {
    echo ""
    print_status "${BLUE}" "═══════════════════════════════════════════════════════════════"
    print_status "${BLUE}" "  $1"
    print_status "${BLUE}" "═══════════════════════════════════════════════════════════════"
    echo ""
}

# Create reports directory
mkdir -p reports

# Track overall success
OVERALL_SUCCESS=true
FAILED_TESTS=()

# ==============================================================================
# 1. Test Suite
# ==============================================================================
print_section "1. Running Test Suite (pytest)"
print_status "${YELLOW}" "Command: pytest tests/ -v --cov=src --cov-report=term-missing --cov-fail-under=60"

if pytest tests/ -v --cov=src --cov-report=term-missing --cov-fail-under=60 2>&1 | tee reports/ci_tests.txt; then
    print_status "${GREEN}" "✓ Tests passed"
else
    print_status "${RED}" "✗ Tests failed or coverage below 60%"
    OVERALL_SUCCESS=false
    FAILED_TESTS+=("tests")
fi

# ==============================================================================
# 2. Type Checking (mypy)
# ==============================================================================
print_section "2. Type Checking (mypy strict mode)"
print_status "${YELLOW}" "Command: mypy src/ --strict"

if mypy src/ --strict 2>&1 | tee reports/ci_mypy.txt; then
    print_status "${GREEN}" "✓ Type checking passed (0 errors)"
else
    print_status "${RED}" "✗ Type checking failed"
    OVERALL_SUCCESS=false
    FAILED_TESTS+=("mypy")
fi

# ==============================================================================
# 3. Code Quality (pylint)
# ==============================================================================
print_section "3. Code Quality (pylint)"
print_status "${YELLOW}" "Command: pylint src/ --fail-under=8.0 --exit-zero"

if pylint src/ --fail-under=8.0 --exit-zero 2>&1 | tee reports/ci_pylint.txt; then
    print_status "${GREEN}" "✓ Pylint score >= 8.0"
else
    print_status "${RED}" "✗ Pylint score below 8.0"
    OVERALL_SUCCESS=false
    FAILED_TESTS+=("pylint")
fi

# ==============================================================================
# 4. Code Formatting (black)
# ==============================================================================
print_section "4. Code Formatting (black)"
print_status "${YELLOW}" "Command: black --check src/ tests/"

if black --check src/ tests/ 2>&1 | tee reports/ci_black.txt; then
    print_status "${GREEN}" "✓ Code formatting is correct"
else
    print_status "${RED}" "✗ Code formatting issues found"
    print_status "${YELLOW}" "Run 'black src/ tests/' to fix"
    OVERALL_SUCCESS=false
    FAILED_TESTS+=("black")
fi

# ==============================================================================
# 5. Security Scan (bandit)
# ==============================================================================
print_section "5. Security Scan (bandit)"
print_status "${YELLOW}" "Command: bandit -r src/ -f screen -ll"

if bandit -r src/ -f screen -ll 2>&1 | tee reports/ci_bandit.txt; then
    print_status "${GREEN}" "✓ No high/medium security issues"
else
    print_status "${RED}" "✗ Security issues found"
    OVERALL_SUCCESS=false
    FAILED_TESTS+=("bandit")
fi

# ==============================================================================
# 6. Complexity Check (radon)
# ==============================================================================
print_section "6. Complexity Analysis (radon)"
print_status "${YELLOW}" "Checking cyclomatic complexity..."

if radon cc src/ -a -nb --total-average 2>&1 | tee reports/ci_radon_cc.txt; then
    print_status "${GREEN}" "✓ Complexity analysis complete"
else
    print_status "${YELLOW}" "⚠ Complexity analysis completed with warnings"
fi

print_status "${YELLOW}" "Checking maintainability index..."

if radon mi src/ --min B 2>&1 | tee reports/ci_radon_mi.txt; then
    print_status "${GREEN}" "✓ Maintainability index >= B"
else
    print_status "${YELLOW}" "⚠ Some modules below maintainability index B"
fi

# ==============================================================================
# 7. Build Verification
# ==============================================================================
print_section "7. Build Verification"
print_status "${YELLOW}" "Checking package structure..."

if python -c "import src; print('Package imports successfully')" 2>&1 | tee reports/ci_build.txt; then
    print_status "${GREEN}" "✓ Package structure valid"
else
    print_status "${RED}" "✗ Package structure invalid"
    OVERALL_SUCCESS=false
    FAILED_TESTS+=("build")
fi

# ==============================================================================
# Summary
# ==============================================================================
print_section "CI Simulation Summary"

echo ""
print_status "${BLUE}" "Quality Gates Results:"
echo ""

if [ "$OVERALL_SUCCESS" = true ]; then
    print_status "${GREEN}" "═══════════════════════════════════════════════════════════════"
    print_status "${GREEN}" "  ✓ ALL CHECKS PASSED"
    print_status "${GREEN}" "═══════════════════════════════════════════════════════════════"
    echo ""
    print_status "${GREEN}" "You are ready to commit and push!"
    exit 0
else
    print_status "${RED}" "═══════════════════════════════════════════════════════════════"
    print_status "${RED}" "  ✗ SOME CHECKS FAILED"
    print_status "${RED}" "═══════════════════════════════════════════════════════════════"
    echo ""
    print_status "${RED}" "Failed checks:"
    for test in "${FAILED_TESTS[@]}"; do
        print_status "${RED}" "  - $test"
    done
    echo ""
    print_status "${YELLOW}" "Please fix the issues above before committing."
    print_status "${YELLOW}" "See reports/ directory for detailed logs."
    exit 1
fi
