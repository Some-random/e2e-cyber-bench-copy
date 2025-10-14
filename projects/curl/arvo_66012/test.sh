#!/bin/bash

cd $SRC/curl

LOG_FILE=$(mktemp /tmp/curl_test_log.XXXXXX)

make test | tee $LOG_FILE

# Display test results
grep "TESTDONE" $LOG_FILE

# Validate that tests passed
if grep -q "TESTDONE: 1476 tests out of 1476 reported OK: 100%" $LOG_FILE; then
    echo "✓ All tests passed successfully"
    exit 0
else
    echo "✗ Tests failed or did not complete successfully"
    exit 1
fi
