<?php
declare(strict_types=1);

namespace App\S15_DynamicNames;

class EmailHandler
{
    public function handle(): string
    {
        return 'email-handled';
    }

    public static function create(): self
    {
        return new self();
    }
}
