<?php

namespace App\Controllers;

use CodeIgniter\Router\Attributes\Restrict;

#[Restrict(environment: 'production')]
class Panel
{
    public function index() {}
}
