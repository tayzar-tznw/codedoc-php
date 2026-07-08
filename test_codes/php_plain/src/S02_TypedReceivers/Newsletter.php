<?php
declare(strict_types=1);

namespace App\S02_TypedReceivers;

class Newsletter
{
    public function send(): string
    {
        return 'newsletter-sent';
    }
}
