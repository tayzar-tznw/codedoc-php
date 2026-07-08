<?php
declare(strict_types=1);

namespace App\Service;

use App\Http\Client;
use App\Utility\Hash;
use App\Utility\Text;

class AppUtilityConsumer
{
    public function makeSlug(string $title): string
    {
        return Text::slug($title);
    }

    public function readSetting(array $config): mixed
    {
        return Hash::get($config, 'mail.host');
    }

    public function fetchStatus(): string
    {
        $client = new Client();

        return $client->get('https://example.test/status');
    }
}
