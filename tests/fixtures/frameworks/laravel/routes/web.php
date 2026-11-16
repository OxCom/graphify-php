<?php

use App\Http\Controllers\PhotoController;
use App\Http\Controllers\PostController;
use App\Http\Controllers\ReportController;
use App\Http\Controllers\ShowDashboard;
use App\Http\Controllers\UserController;
use Illuminate\Support\Facades\Route;

Route::get('/', fn () => view('welcome'));

Route::get('/users/{user}', [UserController::class, 'show'])->name('users.show')->middleware('auth');
Route::post('/users', '\App\Http\Controllers\UserController@store')->middleware(['auth', 'throttle:6,1']);
Route::get('/short', 'UserController@index');
Route::get('/dashboard', ShowDashboard::class)->middleware('verified');
Route::put('/users/{user}', [UserController::class, 'update'])->can('update', 'user');
Route::get(PREFIX . '/dynamic', [UserController::class, 'show']);

Route::middleware(['auth'])->prefix('/admin/')->name('admin.')->group(function () {
    Route::get('users', [UserController::class, 'index'])->name('users');
    Route::prefix('reports')->group(function () {
        Route::get('/{id}', [UserController::class, 'report'])->where('id', '.*')->withoutMiddleware('auth');
    });
    require __DIR__.'/admin.php';
});

Route::controller(PostController::class)->group(function () {
    Route::get('/posts/{post}', 'show');
    Route::post('/posts', 'store')->middleware('can:create,App\Models\Post');
});

Route::group(['prefix' => 'legacy', 'middleware' => 'guest', 'as' => 'legacy.'], function () {
    Route::match(['get', 'post'], '/login', [UserController::class, 'login'])->name('login');
});

Route::resource('photos', PhotoController::class)->only(['index', 'show']);
Route::resource('people', PhotoController::class);

foreach (['a', 'b'] as $letter) {
    Route::get('/letters/' . $letter, [UserController::class, 'show']);
}

Route::view('/welcome', 'welcome');
Route::customMacro('/macro');

// docs-check laravel B.6, B.7: a controller group, route-level prefix after the verb.
Route::controller(ReportController::class)->prefix('reports')->name('reports.')->group(function () {
    Route::get('/', 'index')->name('index');
    Route::get('/export', 'export')->name('export')->prefix('v2');
    Route::get('/ping', 'ping');
});
// docs-check laravel B.4: `any` on 13.x includes QUERY.
Route::any('/hook', [ReportController::class, 'hook']);
