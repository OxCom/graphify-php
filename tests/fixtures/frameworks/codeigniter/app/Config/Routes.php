<?php

use App\Controllers\Api\Tokens;
use CodeIgniter\Router\RouteCollection;

/**
 * @var RouteCollection $routes
 */
$routes->get('/', 'Home::index');
$routes->get('users/(:num)', 'Users::show/$1');
$routes->get('files/(:any)', 'Files::serve/$1');
$routes->get('admin/users', 'Admin\Users::index');
$routes->get('legacy', '\Legacy\Old::run');
$routes->post('tokens', [Tokens::class, 'create']);
$routes->get('ping', static fn () => 'pong');
$routes->get('users/(:segment)', 'Users::other/$1');
$routes->get('account', 'Account::index', ['filter' => 'session']);
$routes->add('contact', 'Contact::form');
$routes->match(['GET', 'post'], 'search', 'Search::run');
$routes->cli('jobs', 'Jobs::run');
$routes->view('about', 'pages/about');
$routes->addRedirect('old', 'new');

$routes->group('reports', ['filter' => 'group:admin'], static function ($routes) {
    $routes->get('daily', 'Reports::daily', ['filter' => 'permission:reports.read']);
    $routes->group('x', ['namespace' => 'App\Controllers\Admin'], static function ($routes) {
        $routes->get('y', 'Users::index');
    });
});

$routes->resource('photos');
$routes->presenter('albums', ['placeholder' => '(:num)', 'only' => 'index,show']);

$routes->environment('development', static function ($routes) {
    $routes->get('debug', 'Debug::index');
});
$routes->get('panel', 'Panel::index', ['hostname' => 'admin.example.com']);

service('auth')->routes($routes);
