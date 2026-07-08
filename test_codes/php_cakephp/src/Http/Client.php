<?php
declare(strict_types=1);

namespace App\Http;

class Client
{
    public function get(string $url, array|string $data = [], array $options = []): string
    {
        return 'app-client:' . $url;
    }
}
