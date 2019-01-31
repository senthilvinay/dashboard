(function() {
    "use strict";

    angular
        .module("adminPcam")
        .controller("ClientViewController", ClientViewController);

    /** @ngInject */
    function ClientViewController(
        ChartConfigService,
        NgTableParams,
        $stateParams,
        $http,
        $log,
        $filter
    ) {
        var vm = this;
        vm.appName = $stateParams.appName;
        vm.apmId = $stateParams.apmid;

        //$log.log(vm.apmId + "   " + vm.appName);

        vm.dateRange = {
            startDate: moment().startOf("day"),
            endDate: moment()
        };



       vm.startdate = $filter('date')(vm.dateRange.startDate._d,'yyyy-MM-dd HH:mm:ss');
       vm.enddate = $filter('date')(vm.dateRange.endDate._d,'yyyy-MM-dd HH:mm:ss');



        vm.options = {
            applyClass: "btn-primary",
            opens: "left",
            local: {
                applyLabel: "Apply",
                fromLabel: "From",
                format: "DD-MM-YYYY",
                toLabel: "To",
                cancelLabel: "Cancel",
                customRangeLabel: "Custom range"
            },
            ranges: {
                Today: [moment().startOf("day"), moment()],
                Yesterday: [moment().subtract(1, "days"), moment().subtract(1, "days")],
                "Last 7 days": [moment().subtract(7, "days"), moment()],
                "Last 30 days": [moment().subtract(30, "days"), moment()],
                "Last 90 days": [moment().subtract(90, "days"), moment()],
                "Last 6 months": [moment().subtract(180, "days"), moment()],
                "Last 365 days": [moment().subtract(365, "days"), moment()]
            }
        };


        vm.dountChartOptions = angular.copy(ChartConfigService.dountChartOptions);
        vm.barChartOptions = angular.copy(ChartConfigService.multiBarChartOptions);
        vm.lineChartOptions = angular.copy(ChartConfigService.lineChartOptions);
        vm.barChartOptions.chart.height = 700;
        vm.barChartOptions.chart.stacked = true;
        vm.dountChartOptions.chart.pie = {
            startAngle: function(d) {
                return d.startAngle;
            },
            endAngle: function(d) {
                return d.endAngle;
            }
        };
        vm.dountChartOptions.chart.height = 260;
        vm.dountChartOptions.chart.title = "";


$http.get('http://e2e-influxdb.kube-dev.swissre.com/query?pretty=true&db=Monitoring&q=' + encodeURIComponent("SELECT * as time from monitorResults WHERE time > now() - 24h and subMeasurement =~ /[^Grand_Total]/ and apmid='" + vm.apmId + "' GROUP BY * ORDER BY ASC LIMIT 1"))
        //$http.get('http://localhost:3000/assets/JSON/moniter.json')
            .then(function success(response) {

                vm.getRet = response.data.results[0].series;
                vm.totalCount = vm.getRet.length;
                vm.getTags = [];
                vm.getMonCount = [];
                vm.getMearCount = [];
                vm.totalMont = [];
                vm.appid_count = [];
                vm.appCritFail = [];
                vm.monitorList = [];
                vm.getHostCount = [];
                vm.getHostList = [];
                vm.getTransCount = [];
                vm.getTransList = [];
                vm.getmonName = [];
                vm.metricsReportData = [];
                vm.instance = [];


                var all_mon_data1 = {};

                for (vm.key in vm.getRet) {
                    if (vm.getRet[vm.key]["tags"]) {
                        vm.getTags.push(Object.values(vm.getRet[vm.key]["tags"]));
                        vm.getMonCount.push(vm.getRet[vm.key]["tags"]["apmid"]);
                        vm.getMearCount.push(vm.getRet[vm.key]["tags"]["measurement"]);
                        vm.getHostCount.push(vm.getRet[vm.key]["tags"]["hostname"]);
                        vm.getTransCount.push(vm.getRet[vm.key]["tags"]["subMeasurement"]);
                        vm.getmonName.push(vm.getRet[vm.key]["tags"]["monitorName"]);

                        all_mon_data1.date = vm.getRet[vm.key]["values"][0][0];
                        all_mon_data1.apmid = vm.getRet[vm.key]["tags"]["resource"];

                        var array = all_mon_data1.apmid.split('.')
                        vm.instance.push(array[1]);
                        $log.log(vm.instance.name);
                        all_mon_data1.environment = vm.getRet[vm.key]["tags"]["environment"];
                        all_mon_data1.hostname = vm.getRet[vm.key]["tags"]["hostname"];
                        all_mon_data1.location = vm.getRet[vm.key]["tags"]["location"];
                        all_mon_data1.measurement = vm.getRet[vm.key]["tags"]["measurement"];
                        all_mon_data1.subMeasurement = vm.getRet[vm.key]["tags"]["subMeasurement"];
                        all_mon_data1.result = vm.getRet[vm.key]["tags"]["result"];
                        vm.metricsReportData.push(all_mon_data1);


                       if (vm.getRet[vm.key]["tags"]["result"] === "Crit_Failed") {
                            vm.appCritFail.push(vm.getRet[vm.key]["tags"]["apmid"]);
                        }
                    }
                }



               vm.maintCount = vm.getTags.filter(function(arr) {
                    return arr.indexOf("MAINT") > -1;
                });

                vm.failedCount = vm.getTags.filter(function(arr) {
                    return arr.indexOf("Crit_Failed") > -1;
                });

                vm.successCount = vm.getTags.filter(function(arr) {
                    return arr.indexOf("OK") > -1;
                });

                vm.maintLen = vm.maintCount.length;
                vm.failedLen = vm.failedCount.length;
                vm.successLen = vm.successCount.length;


               vm.getmon = {};
               vm.getmon = vm.getmonName.reduce(function(prev, cur) {
               prev[cur] = (prev[cur] || 0) + 1;
               return prev;
               }, {});

               for (vm.key in vm.getmon) {
               //$log.log('Each Key items' + vm.key);
                var tempmonName = {};
                tempmonName.name = vm.key;
                //tempmonName.name1 = 'robothbP__.test';
                vm.monitorList.push(tempmonName);

               }
               vm.instances = [];

              $log.log(vm.instance);


               for (var j = 0; j < vm.instance.length; j++)
               {
                 $log.log('XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'+vm.instance[j]);
                 var tempmonName1 = {};
                 tempmonName1.name = vm.instance[j];
                    //tempmonName.name1 = 'robothbP__.test';
                   vm.instances.push(tempmonName1);

               }




               vm.mearCountLen = vm.monitorList.length;

                vm.getHostFil = {};
                vm.getHostFil = vm.getHostCount.filter(function(item, pos) {

                    return vm.getHostCount.indexOf(item) == pos;
                });

                vm.getTransFil = {};
                vm.getTransFil = vm.getTransCount.filter(function(item, pos) {
                    return vm.getTransCount.indexOf(item) == pos;
                });

                for (var j = 0; j < vm.getHostFil.length; j++) {
                    var tempName = {};
                    tempName.name = vm.getHostFil[j];
                    vm.getHostList.push(tempName);
                }

                for (var k = 0; k < vm.getTransFil.length; k++) {
                    var tempTran = {};
                    tempTran.name = vm.getTransFil[k];
                    vm.getTransList.push(tempTran);
                }



                vm.selectedMonitor = vm.monitorList[0];
                vm.getdata();
                /*sample Data dount*/
                vm.dataDount = [{
                        key: "Success",
                        y: vm.successLen,
                        color: "#61b861"
                    },
                    {
                        key: "Fail",
                        y: vm.failedLen,
                        color: "#ff0000"
                    },
                    {
                        key: "Maintenance",
                        y: vm.maintLen,
                        color: "#120000"
                    }
                ];
            });





$http.get('http://e2e-influxdb.kube-dev.swissre.com/query?pretty=true&db=Monitoring&q=' + encodeURIComponent("SELECT *  from monitorResults WHERE time > now() - 24h and apmid='" + vm.apmId + "' ORDER BY ASC"))
        //$http.get('http://localhost:3000/assets/JSON/moniter.json')
            .then(function success(response1) {



                vm.getRetTab = response1.data.results[0].series;
                vm.getmondetail = [];



 for (vm.key in vm.getRetTab)
 {
    $log.log('Keyss'+vm.getRetTab[vm.key]["values"].length);

         for (var j = 0; j < vm.getRetTab[vm.key]["values"].length; j++) {
                    var tempName1 = {};
                   //$log.log('Data.....'+vm.getRetTab[vm.key]["values"][j][13]);
                     tempName1.date = vm.getRetTab[vm.key]["values"][j][0];
                     tempName1.apmid = vm.getRetTab[vm.key]["values"][j][2];
                     tempName1.environment = vm.getRetTab[vm.key]["values"][j][4];
                     tempName1.hostname = vm.getRetTab[vm.key]["values"][j][5];
                     tempName1.location = vm.getRetTab[vm.key]["values"][j][8];
                     tempName1.measurement1 = vm.getRetTab[vm.key]["values"][j][9];
                     tempName1.monitorName = vm.getRetTab[vm.key]["values"][j][10];
                     tempName1.resource = vm.getRetTab[vm.key]["values"][j][13];
                     tempName1.submeasurement = vm.getRetTab[vm.key]["values"][j][15];
                     tempName1.result = vm.getRetTab[vm.key]["values"][j][14];
                    vm.getmondetail.push(tempName1);
                }

 }
        vm.tableParams = new NgTableParams({}, {
            dataset: vm.getmondetail
        });

                            });


//$log.log('XXXXXXXXXXXXXXXXXXXXXXX'+vm.getmondetail);


vm.getdata = function() {
        vm.options.eventHandlers = {
            'apply.daterangepicker': function (ev, datepicker) {
                //alert('Date Changed')
                vm.barChartData = [];
                vm.startdate = $filter('date')(vm.dateRange.startDate._d,'yyyy-MM-dd HH:mm:ss');
                vm.enddate = $filter('date')(vm.dateRange.endDate._d,'yyyy-MM-dd HH:mm:ss');
                chartservice(vm.startdate,vm.enddate,vm.selectedMonitor.name,vm.selectedHost.name);
        }

            }

        }

    vm.instancecall =  function()
    {
    chartservice(vm.startdate,vm.enddate,vm.selectedMonitor.name,vm.selectedHost.name);
    }

    vm.hostnamecall =  function()
    {
    chartservice(vm.startdate,vm.enddate,vm.selectedMonitor.name,vm.selectedHost.name);
    }

    vm.monitorcall =  function()
    {
    chartservice(vm.startdate,vm.enddate,vm.selectedMonitor.name,vm.selectedHost.name);
    }


function chartservice(sd, ed,monname, hostname) {

           $http.get('http://e2e-influxdb.kube-dev.swissre.com/query?epoch=ms&pretty=true&db=Monitoring&q=' + encodeURIComponent("SELECT mean(value) AS mean_value FROM Monitoring.autogen.monitorResults WHERE time > '" + sd + "' AND time < '" + ed + "' AND monitorName='" + monname + "'  and hostname ='" + hostname + "' and subMeasurement <> 'Grand_Total'    GROUP BY time(30m),subMeasurement,location")).then(function success(response) {

//$log.log('Monitor NameXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'+ vm.selectedMonitor.name);

// $http.get('http://localhost:3000/assets/JSON/chart.json')
                vm.indMeasurement = [];
                vm.indMeaObj = [];
                vm.barChartData = [];

                vm.lineChart = response.data.results[0].series;
                $log.log('JSON Data' + vm.lineChart);
                vm.lineChart.map(function(measurement) {
                    if (

                        vm.indMeasurement.indexOf(measurement.tags.subMeasurement) == -1
                    ) {
                        vm.indMeasurement.push(measurement.tags.subMeasurement);
                        vm.indMeaObj[measurement.tags.subMeasurement] = measurement.values;
                    }
                });


                var numberify = maybeMap(Number);
                for (var keys in vm.indMeaObj) {
                    //ert('XXXXXXXX' + vm.indMeaObj[keys]);
                    var d = new Date(vm.indMeaObj[keys][0][0]);
                    //alert(d)
                    //if(vm.indMeaObj[keys][1][1] === 'null') {vm.indMeaObj[keys][1][1]=0; }
                    vm.barChartData.push({
                        key: keys,
                        values: numberify(vm.indMeaObj[keys]),
                        mean: 250
                    });
                }


});
};


    }

})();

var maybeMap = function maybeMap(fn) {
    return function(x) {
        return Array.isArray(x) ? x.map(maybeMap(fn)) : fn(x);
    };
};
