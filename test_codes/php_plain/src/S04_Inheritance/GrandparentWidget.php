<?php
declare(strict_types=1);

namespace App\S04_Inheritance;

class GrandparentWidget
{
    public function describe(): string
    {
        return 'grandparent-widget';
    }

    public function format(): string
    {
        return 'grandparent-format';
    }

    public function inheritedOnly(): string
    {
        return 'grandparent-only';
    }
}
