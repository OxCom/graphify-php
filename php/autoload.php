<?php

declare(strict_types=1);

/**
 * PSR-4 registrar for the CallGraph namespace.
 *
 * Shipped instead of a composer package so adopting this costs a `bootstrapFiles` line and no
 * dependency resolution: it is analysed inside somebody else's project, whose lockfile is not
 * ours to move.
 */
spl_autoload_register(static function (string $class): void {
    $prefix = 'CallGraph\\';

    if (!str_starts_with($class, $prefix)) {
        return;
    }

    $relative = substr($class, strlen($prefix));
    $path = __DIR__ . '/src/CallGraph/' . str_replace('\\', '/', $relative) . '.php';

    if (!is_file($path)) {
        return;
    }

    require_once $path;
});
