<?php

namespace Config;

use App\Filters\AdminOnly;
use CodeIgniter\Config\Filters as BaseFilters;
use CodeIgniter\Filters\CSRF;
use CodeIgniter\Filters\DebugToolbar;
use CodeIgniter\Filters\ForceHTTPS;
use CodeIgniter\Filters\Honeypot;
use CodeIgniter\Filters\PageCache;
use CodeIgniter\Filters\PerformanceMetrics;

class Filters extends BaseFilters
{
    public array $aliases = [
        'csrf'        => CSRF::class,
        'toolbar'     => DebugToolbar::class,
        'honeypot'    => Honeypot::class,
        'forcehttps'  => ForceHTTPS::class,
        'pagecache'   => PageCache::class,
        'performance' => PerformanceMetrics::class,
        'admin-only'  => AdminOnly::class,
    ];

    public array $required = [
        'before' => ['forcehttps', 'pagecache'],
        'after'  => ['pagecache', 'performance', 'toolbar'],
    ];

    public array $globals = [
        'before' => [
            'csrf' => ['except' => ['api/*', 'tokens']],
        ],
        'after' => ['honeypot'],
    ];

    public array $methods = [
        'POST' => ['honeypot'],
    ];

    public array $filters = [
        'session'    => ['before' => ['account*', 'reports/*']],
        'admin-only' => ['before' => ['users/1'], 'after' => ['users/*']],
    ];
}
