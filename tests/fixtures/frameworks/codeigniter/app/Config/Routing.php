<?php

namespace Config;

use CodeIgniter\Config\Routing as BaseRouting;

class Routing extends BaseRouting
{
    public array $routeFiles = [
        APPPATH . 'Config/Routes.php',
        APPPATH . 'Config/AdminRoutes.php',
    ];

    public string $defaultNamespace = 'App\Controllers';

    public bool $autoRoute = true;

    public bool $useControllerAttributes = true;
}
