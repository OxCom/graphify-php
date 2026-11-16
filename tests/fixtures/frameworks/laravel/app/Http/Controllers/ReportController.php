<?php

namespace App\Http\Controllers;

use App\Models\Report;
use Illuminate\Routing\Attributes\Controllers\Authorize;
use Illuminate\Routing\Attributes\Controllers\Middleware;
use Illuminate\Routing\Attributes\Controllers\WithoutMiddleware;

// docs-check laravel C.10: 13.x controller attributes, class and method targets.
#[Middleware('auth')]
#[Authorize('view-reports', only: ['index'])]
class ReportController extends Controller
{
    public function index() {}

    #[Authorize('export', Report::class)]
    public function export() {}

    #[WithoutMiddleware('auth')]
    public function ping() {}

    public function hook() {}
}
