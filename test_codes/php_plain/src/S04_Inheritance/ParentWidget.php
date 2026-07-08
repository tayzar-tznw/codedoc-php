<?php
declare(strict_types=1);

namespace App\S04_Inheritance;

class ParentWidget extends GrandparentWidget
{
    public function format(): string
    {
        return 'parent-format';
    }

    public function parentOnly(): string
    {
        return 'parent-only';
    }
}
