# iocsh start-up script: an alternative to "softIoc -d db/hvac_test.db -d db/hvac_sim_support.db"
# Run from the ioc/ folder:   softIoc hvac_ioc.cmd
dbLoadRecords("db/hvac_test.db")
dbLoadRecords("db/hvac_sim_support.db")
iocInit
