<?php

use App\Http\Controllers\Api\TokenController;
use Illuminate\Support\Facades\Route;

Route::middleware('auth:sanctum')->get('/user', [TokenController::class, 'user']);
Route::apiResource('tokens', TokenController::class)->except(['update']);
Route::post('/tokens/{token}/revoke', [TokenController::class, 'revoke'])->middleware('signed');
