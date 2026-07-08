<?php
declare(strict_types=1);

namespace App\Service;

use Cake\Http\Client;
use Cake\Utility\Hash;
use Cake\Utility\Text;

class VendorUtilityConsumer
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

        return (string)$client->get('https://example.test/status')->getStringBody();
    }
}
