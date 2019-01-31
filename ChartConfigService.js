'use strict';


angular
.module('adminPcam')
.factory('ChartConfigService', ChartConfigService)

function ChartConfigService($q, $resource) {
  var factory = {};
  Date.prototype.addHours = function (h) {
    this.setHours(this.getHours() + h);
    return this;
  };
  factory.colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

  factory.multiBarChartOptions = {
    chart: {
      color: factory.colors,
      type: 'multiBarChart',
      height: 225,
      margin:{ top: 40,right: 20,bottom: 45,left: 45 },
      stacked: false,
      useInteractiveGuideline: true,
      x: function (d) {
        return d.x;
      },
      y: function (d) {
        return d.y;
      },
      showControls: false,
      transitionDuration: 1000,
      //   xScale : d3.time.scale(), // <-- explicitly set time scale
      xAxis: {
        axisLabel:"Date",
        tickFormat: function (d) {
          return d;
        }
      },

      yAxis: {
       axisLabel:"Y-Axis",
       tickFormat: function (d) {
        return d3.format('.02f')(d);
      }
    }
  }
};

factory.dountChartOptions = {
  chart: {
    color: factory.colors,
    type: 'pieChart',
    height: 350,
    donut: true,
    x: function(d){return d.key;},
    y: function(d){return d.y;},
    showLabels: false,
    showLegend : false,
    // color :['#95B75D', '#1caf9a', '#FEA223'],
    title : 'Overall Status',
    pie: {
      startAngle: function(d) { return d.startAngle/2 -Math.PI/2 },
      endAngle: function(d) { return d.endAngle/2 -Math.PI/2 }
    },
    duration: 500
  }
};
factory.lineChartOptions = {
            chart: {
                type: 'lineChart',
                height: 450,
                margin : {
                    top: 20,
                    right: 20,
                    bottom: 60,
                    left: 65
                },
                x: function(d){
                  return d[0];
                },
                y: function(d){
                  return d[1];
                },

                color: d3.scale.category10().range(),
                duration: 300,
                useInteractiveGuideline: true,
                clipVoronoi: false,

                xAxis: {
                    axisLabel: 'Date',
                    tickFormat: function(d) {
                        //return d3.time.format('%m/%d/%y')(new Date(d/1000))
                          return d3.time.format("%a %b %e %X")(new Date(d));
                    },
                    showMaxMin: false,
                    staggerLabels: true
                },

                yAxis: {
                    axisLabel: 'Y Axis',
                    tickFormat: function(d){
                        return d3.format(',.1')(d);
                    },
                    axisLabelDistance: 20
                }
            }
}

return factory;
}

