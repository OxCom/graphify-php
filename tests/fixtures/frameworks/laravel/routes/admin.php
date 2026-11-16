<?php

use App\Http\Controllers\UserController;
use Illuminate\Support\Facades\Route;

Route::delete('users/{user}', [UserController::class, 'destroy'])->name('users.destroy');
