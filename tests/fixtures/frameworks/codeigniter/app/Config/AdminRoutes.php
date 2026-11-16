<?php

// Loaded after Routes.php: the same path and verb is a duplicate, the first one wins.
$routes->get('users/(:num)', 'Users::late/$1');
