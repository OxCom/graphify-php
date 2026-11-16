<?php

namespace App\Http\Controllers;

use App\Models\User;
use Illuminate\Support\Facades\Gate;

class UserController extends Controller
{
    public function show(User $user)
    {
        $this->authorize('view', $user);
    }

    public function store() {}

    public function index() {}

    public function update(User $user)
    {
        Gate::authorize('update', $user);
        if (Gate::allows('admin')) {
            return;
        }
        $later = fn () => Gate::denies('never-read');
    }

    public function report(string $id) {}

    public function login() {}

    public function destroy(User $user) {}
}
