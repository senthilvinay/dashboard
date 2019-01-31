(function() {
  'use strict';

  angular
    .module('adminPcam')
    .config(routerConfig);

  /** @ngInject */
  function routerConfig($stateProvider, $urlRouterProvider) {
    $stateProvider
      .state('main', {
        url: '/main',
        templateUrl: 'app/main/main.html',
        controller: 'MainController',
        controllerAs: 'main',
        abstract : true
      })
      .state('main.dashboard', {
        url: '/dashboard',
        templateUrl: 'app/dashboard/dashboard.html',
        controller: 'DashboardController',
        controllerAs: 'dash'
      })
      .state('main.clientView', {
        url: '/clientView?:apmid&:appName',
        templateUrl: 'app/clientView/clientView.html',
        controller: 'ClientViewController',
        controllerAs: 'client'
      })
     .state('main.monitoring', {
        url: '/opsview',
        templateUrl: 'app/monitoring/monitoring_operation.html',
        controller: 'MonitoringOperation',
        controllerAs: 'moniCtrl'
      })
      .state('main.monitoringDetail', {
        url: '/opsdetail',
        templateUrl: 'app/monitoring/monitoring_detailPage.html',
        controller: 'MonitoringDetail',
        controllerAs: 'detail'
      });

    $urlRouterProvider.otherwise('/main/dashboard');
  }

})();
