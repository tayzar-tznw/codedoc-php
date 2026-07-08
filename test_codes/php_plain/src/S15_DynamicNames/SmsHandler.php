<?php
declare(strict_types=1);

namespace App\S15_DynamicNames;

class SmsHandler
{
    public function handle(): string
    {
        return 'sms-handled';
    }

    public static function create(): self
    {
        return new self();
    }
}
