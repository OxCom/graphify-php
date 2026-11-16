<?php

namespace App\Http\Controllers;

use Illuminate\Routing\Controllers\HasMiddleware;
use Illuminate\Routing\Controllers\Middleware;

class PostController extends Controller implements HasMiddleware
{
    public static function middleware(): array
    {
        return [
            'auth',
            new Middleware('log', only: ['show']),
            new Middleware('can:edit-posts', except: ['show']),
            function ($request, $next) {
                return $next($request);
            },
        ];
    }

    public function show() {}

    public function store() {}
}
