<?php

namespace App\Controllers;

use CodeIgniter\Router\Attributes\Filter;

class Account
{
    #[Filter(by: 'group', having: ['admin', 'superadmin'])]
    public function index() {}
}
